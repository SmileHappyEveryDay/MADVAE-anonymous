"""
Modified from OpenAI Baselines code to work with multi-agent envs
"""
import numpy as np
from multiprocessing import Process, Pipe
from abc import ABC, abstractmethod


class CloudpickleWrapper(object):
    """
    Uses cloudpickle to serialize contents (otherwise multiprocessing tries to use pickle)
    """

    def __init__(self, x):
        self.x = x

    def __getstate__(self):
        import cloudpickle
        return cloudpickle.dumps(self.x)

    def __setstate__(self, ob):
        import pickle
        self.x = pickle.loads(ob)


def tile_images(img_nhwc):
    """
    Tile N images into one big PxQ image
    (P,Q) are chosen to be as close as possible, and if N
    is square, then P=Q.
    input: img_nhwc, list or array of images, ndim=4 once turned into array
        n = batch index, h = height, w = width, c = channel
    returns:
        bigim_HWc, ndarray with ndim=3
    """
    img_nhwc = np.asarray(img_nhwc)
    N, h, w, c = img_nhwc.shape
    H = int(np.ceil(np.sqrt(N)))
    W = int(np.ceil(float(N)/H))
    img_nhwc = np.array(list(img_nhwc) + [img_nhwc[0]*0 for _ in range(N, H*W)])
    img_HWhwc = img_nhwc.reshape(H, W, h, w, c)
    img_HhWwc = img_HWhwc.transpose(0, 2, 1, 3, 4)
    img_Hh_Ww_c = img_HhWwc.reshape(H*h, W*w, c)
    return img_Hh_Ww_c


class ShareVecEnv(ABC):
    """
    An abstract asynchronous, vectorized environment.
    Used to batch data from multiple copies of an environment, so that
    each observation becomes an batch of observations, and expected action is a batch of actions to
    be applied per-environment.
    """
    closed = False
    viewer = None

    metadata = {
        'render.modes': ['human', 'rgb_array']
    }

    def __init__(self, num_envs, observation_space, share_observation_space, action_space):
        self.num_envs = num_envs
        self.observation_space = observation_space
        self.share_observation_space = share_observation_space
        self.action_space = action_space

    @abstractmethod
    def reset(self):
        """
        Reset all the environments and return an array of
        observations, or a dict of observation arrays.

        If step_async is still doing work, that work will
        be cancelled and step_wait() should not be called
        until step_async() is invoked again.
        """
        pass

    @abstractmethod
    def step_async(self, actions):
        """
        Tell all the environments to start taking a step
        with the given actions.
        Call step_wait() to get the results of the step.

        You should not call this if a step_async run is
        already pending.
        """
        pass

    @abstractmethod
    def step_wait(self):
        """
        Wait for the step taken with step_async().

        Returns (obs, rews, dones, infos):
         - obs: an array of observations, or a dict of
                arrays of observations.
         - rews: an array of rewards
         - dones: an array of "episode done" booleans
         - infos: a sequence of info objects
        """
        pass

    def close_extras(self):
        """
        Clean up the  extra resources, beyond what's in this base class.
        Only runs when not self.closed.
        """
        pass

    def close(self):
        if self.closed:
            return
        if self.viewer is not None:
            self.viewer.close()
        self.close_extras()
        self.closed = True

    def step(self, actions):
        """
        Step the environments synchronously.

        This is available for backwards compatibility.
        """
        self.step_async(actions)
        return self.step_wait()

    def render(self, mode='human'):
        imgs = self.get_images()
        bigimg = tile_images(imgs)
        if mode == 'human':
            self.get_viewer().imshow(bigimg)
            return self.get_viewer().isopen
        elif mode == 'rgb_array':
            return bigimg
        else:
            raise NotImplementedError

    def get_images(self):
        """
        Return RGB images from each environment
        """
        raise NotImplementedError

    def get_viewer(self):
        if self.viewer is None:
            from gym.envs.classic_control import rendering
            self.viewer = rendering.SimpleImageViewer()
        return self.viewer


def shareworker(remote, parent_remote, env_fn_wrapper):
    parent_remote.close()
    env = env_fn_wrapper.x()
    while True:
        cmd, data = remote.recv()
        if cmd == 'step':
            ob, s_ob, reward, done, info, available_actions = env.step(data)
            if 'bool' in done.__class__.__name__:
                if done:
                    ob, s_ob, available_actions = env.reset()
            else:
                if np.all(done):
                    ob, s_ob, available_actions = env.reset()

            remote.send((ob, s_ob, reward, done, info, available_actions))
        elif cmd == 'reset':
            ob, s_ob, available_actions = env.reset()
            remote.send((ob, s_ob, available_actions))
        elif cmd == 'reset_task':
            ob = env.reset_task()
            remote.send(ob)
        elif cmd == 'render':
            if data == "rgb_array":
                fr = env.render(mode=data)
                remote.send(fr)
            elif data == "human":
                env.render(mode=data)
        elif cmd == 'close':
            env.close()
            remote.close()
            break
        elif cmd == 'get_spaces':
            remote.send(
                (env.observation_space, env.share_observation_space, env.action_space))
        elif cmd == 'render_vulnerability':
            fr = env.render_vulnerability(data)
            remote.send((fr))
        else:
            raise NotImplementedError


class ShareSubprocVecEnv(ShareVecEnv):
    def __init__(self, env_fns, spaces=None):
        """
        envs: list of gym environments to run in subprocesses
        """
        self.waiting = False
        self.closed = False
        nenvs = len(env_fns)
        self.remotes, self.work_remotes = zip(*[Pipe() for _ in range(nenvs)])
        self.ps = [Process(target=shareworker, args=(work_remote, remote, CloudpickleWrapper(env_fn)))
                   for (work_remote, remote, env_fn) in zip(self.work_remotes, self.remotes, env_fns)]
        for p in self.ps:
            p.daemon = True  # if the main process crashes, we should not cause things to hang
            p.start()
        for remote in self.work_remotes:
            remote.close()
        self.remotes[0].send(('get_spaces', None))
        observation_space, share_observation_space, action_space = self.remotes[0].recv(
        )
        ShareVecEnv.__init__(self, len(env_fns), observation_space,
                             share_observation_space, action_space)

    def step_async(self, actions):
        for remote, action in zip(self.remotes, actions):
            remote.send(('step', action))
        self.waiting = True

    def step_wait(self):
        results = [remote.recv() for remote in self.remotes]
        self.waiting = False
        obs, share_obs, rews, dones, infos, available_actions = zip(*results)
        return np.stack(obs), np.stack(share_obs), np.stack(rews), np.stack(dones), infos, np.stack(available_actions)

    def reset(self):
        for remote in self.remotes:
            remote.send(('reset', None))
        results = [remote.recv() for remote in self.remotes]
        obs, share_obs, available_actions = zip(*results)
        return np.stack(obs), np.stack(share_obs), np.stack(available_actions)

    def reset_task(self):
        for remote in self.remotes:
            remote.send(('reset_task', None))
        return np.stack([remote.recv() for remote in self.remotes])

    def close(self):
        if self.closed:
            return
        if self.waiting:
            for remote in self.remotes:
                remote.recv()
        for remote in self.remotes:
            remote.send(('close', None))
        for p in self.ps:
            p.join()
        self.closed = True


import numpy as np
from multiprocessing import Process, Pipe
import torch

class HybridVecEnv:
    """混合向量化环境：少量进程，每个进程多环境"""
    def __init__(self, env_fns, n_procs=12):
        """
        Args:
            env_fns: 环境创建函数列表
            n_procs: 实际使用的进程数
        """
        self.waiting = False
        self.closed = False
        self.total_envs = len(env_fns)
        self.n_procs = min(n_procs, self.total_envs)
        self.envs_per_proc = self.total_envs // self.n_procs
        
        # 创建管道
        self.remotes, self.work_remotes = zip(*[Pipe() for _ in range(self.n_procs)])
        self.ps = []
        for proc_idx in range(self.n_procs):
            start_idx = proc_idx * self.envs_per_proc
            end_idx = start_idx + self.envs_per_proc
            env_group = env_fns[start_idx:end_idx]
            process = Process(
                target=self.worker,
                args=(self.work_remotes[proc_idx], self.remotes[proc_idx], env_group)
            )
            process.daemon = True
            process.start()
            self.ps.append(process)
        for remote in self.work_remotes:
            remote.close()
    
    @staticmethod
    def worker(remote, parent_remote, env_fns):
        parent_remote.close()
        envs = [fn() for fn in env_fns]
        try:
            while True:
                cmd, data = remote.recv()
                if cmd == 'step':
                    # 对应一个 step 调用，假设每个 env.step(a) 返回 (obs, share_obs, reward, done, info, available_actions)
                    results = [env.step(a) for env, a in zip(envs, data)]
                    # 整理返回值：分组（此处务必保证返回数据结构和外部接口匹配）
                    # 假设外部只需要 obs、share_obs 和 available_actions（其它信息在本实验中不需要）
                    obs, share_obs, _, _, _, available_actions = zip(*results)
                    # 返回每个子进程管理的各个环境的数据
                    remote.send((obs, share_obs, available_actions))
                elif cmd == 'reset':
                    # 每个环境的 reset 返回 (obs, share_obs, available_actions)
                    results = [env.reset() for env in envs]
                    obs, share_obs, available_actions = zip(*results)
                    remote.send((obs, share_obs, available_actions))
                elif cmd == 'close':
                    for env in envs:
                        env.close()
                    remote.close()
                    break
                else:
                    raise NotImplementedError
        except Exception as e:
            print(f"Worker异常: {e}")
            raise

    def reset(self):
        for remote in self.remotes:
            remote.send(('reset', None))
        results = [remote.recv() for remote in self.remotes]
        obs_all, share_obs_all, avail_actions_all = [], [], []
        for obs, share_obs, avail_actions in results:
            obs_all.extend(obs)
            share_obs_all.extend(share_obs)
            avail_actions_all.extend(avail_actions)
        # 返回三个元素
        return obs_all, share_obs_all, avail_actions_all

    def step(self, actions):
        self.step_async(actions)
        return self.step_wait()
    
    def step_async(self, actions):
        action_chunks = np.array_split(actions, self.n_procs)
        for remote, action_chunk in zip(self.remotes, action_chunks):
            remote.send(('step', action_chunk))
        self.waiting = True
        
    def step_wait(self):
        results = [remote.recv() for remote in self.remotes]
        self.waiting = False
        obs_all, share_obs_all, avail_actions_all = [], [], []
        for obs, share_obs, avail_actions in results:
            obs_all.extend(obs)
            share_obs_all.extend(share_obs)
            avail_actions_all.extend(avail_actions)
        return obs_all, share_obs_all, avail_actions_all

    def close(self):
        if self.closed:
            return
        if self.waiting:
            for remote in self.remotes:
                remote.recv()
        for remote in self.remotes:
            remote.send(('close', None))
        for p in self.ps:
            p.join()
        self.closed = True