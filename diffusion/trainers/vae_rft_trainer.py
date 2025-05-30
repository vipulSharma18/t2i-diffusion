"""
Same as RFT trainer but with a VAE
"""

import sys
sys.path.append('./owl-vaes/')
from owl_vaes.utils.proxy_init import CombinedModule
from owl_vaes.configs import Config as VAEConfig

import torch
from ema_pytorch import EMA
import wandb
import torch.nn.functional as F
from torch.nn.parallel import DistributedDataParallel as DDP
import torch.distributed as dist
import einops as eo

from .base import BaseTrainer

from ..utils import freeze, Timer, find_unused_params
from ..schedulers import get_scheduler_cls
from ..models import get_model_cls
from ..sampling import get_sampler_cls
from ..data import get_loader
from ..utils.logging import LogHelper, to_wandb
from ..muon import init_muon

class VAERFTTrainer(BaseTrainer):
    """
    Trainer for rectified flow transformer

    :param train_cfg: Configuration for training
    :param logging_cfg: Configuration for logging
    :param model_cfg: Configuration for model
    :param global_rank: Rank across all devices.
    :param local_rank: Rank for current device on this process.
    :param world_size: Overall number of devices
    """
    def __init__(self,*args,**kwargs):  
        super().__init__(*args,**kwargs)

        model_id = self.model_cfg.model_id
        self.model = get_model_cls(model_id)(self.model_cfg)

        self.ema = None
        self.opt = None
        self.scheduler = None
        self.scaler = None

        self.total_step_counter = 0

        # Initialize VAE
        vae_t_cfg, vae_r_cfg = self.train_cfg.vae_cfg_path_or_paths
        vae_t_ckpt_path, vae_r_ckpt_path = self.train_cfg.vae_ckpt_path_or_paths

        vae_t_cfg = VAEConfig.from_yaml(vae_t_cfg).model
        vae_r_cfg = VAEConfig.from_yaml(vae_r_cfg).model

        self.vae = CombinedModule(vae_t_cfg, vae_r_cfg)
        self.vae.load_ckpt(vae_t_ckpt_path, vae_r_ckpt_path)
        self.vae.scale = self.train_cfg.vae_scale

    def save(self):
        save_dict = {
            'model' : self.model.state_dict(),
            'ema' : self.ema.state_dict(),
            'opt' : self.opt.state_dict(),
            'scaler' : self.scaler.state_dict(),
            'steps': self.total_step_counter
        }
        if self.scheduler is not None:
            save_dict['scheduler'] = self.scheduler.state_dict()
        super().save(save_dict)
    
    def load(self):
        if self.train_cfg.resume_ckpt is not None:
            save_dict = super().load(self.train_cfg.resume_ckpt)
        else:
            return
        
        self.model.load_state_dict(save_dict['model'])
        self.ema.load_state_dict(save_dict['ema'])
        self.opt.load_state_dict(save_dict['opt'])
        if self.scheduler is not None and 'scheduler' in save_dict:
            self.scheduler.load_state_dict(save_dict['scheduler'])
        self.scaler.load_state_dict(save_dict['scaler'])
        self.total_step_counter = save_dict['steps']

    def train(self):
        torch.cuda.set_device(self.local_rank)

        # Prepare model and ema
        self.model = self.model.cuda().train()
        if self.world_size > 1:
            self.model = DDP(self.model, device_ids=[self.local_rank])
        
        self.vae = self.vae.cuda().bfloat16().eval()
        self.vae.transformer.encoder = torch.compile(self.vae.transformer.encoder)

        self.ema = EMA(
            self.model,
            beta = 0.999,
            update_after_step = 0,
            update_every = 1
        )

        def get_ema_core():
            if self.world_size > 1:
                return self.ema.ema_model.module.core
            else:
                return self.ema.ema_model.core

        # Set up optimizer and scheduler
        if self.train_cfg.opt.lower() == "muon":
            self.opt = init_muon(self.model, rank=self.rank,world_size=self.world_size,**self.train_cfg.opt_kwargs)
        else:
            self.opt = getattr(torch.optim, self.train_cfg.opt)(self.model.parameters(), **self.train_cfg.opt_kwargs)

        if self.train_cfg.scheduler is not None:
            self.scheduler = get_scheduler_cls(self.train_cfg.scheduler)(self.opt, **self.train_cfg.scheduler_kwargs)

        # Grad accum setup and scaler
        accum_steps = self.train_cfg.target_batch_size // self.train_cfg.batch_size
        accum_steps = max(1, accum_steps)
        self.scaler = torch.amp.GradScaler()
        ctx = torch.amp.autocast('cuda',torch.bfloat16)

        # Timer reset
        timer = Timer()
        timer.reset()
        metrics = LogHelper()
        if self.rank == 0:
            wandb.watch(self.get_module(), log = 'all')
        
        # Dataset setup
        loader = get_loader(self.train_cfg.data_id, self.train_cfg.batch_size)
        sampler = get_sampler_cls(self.train_cfg.sampler_id)()

        local_step = 0
        for _ in range(self.train_cfg.epochs):
            for batch in loader:
                with torch.no_grad():
                    batch = batch.cuda().bfloat16()
                    batch = self.vae.encode(batch)
                    batch = batch / self.train_cfg.vae_scale
                with ctx:
                    loss = self.model(batch) / accum_steps

                self.scaler.scale(loss).backward()

                metrics.log('diffusion_loss', loss)

                local_step += 1
                if local_step % accum_steps == 0:
                    # Updates
                    if self.train_cfg.opt.lower() != "muon":
                        self.scaler.unscale_(self.opt)
                        torch.nn.utils.clip_grad_norm_(self.model.parameters(), max_norm=1.0)

                    self.scaler.step(self.opt)
                    self.opt.zero_grad()

                    self.scaler.update()

                    if self.scheduler is not None:
                        self.scheduler.step()
                    self.ema.update()

                    # Do logging
                    with torch.no_grad():
                        wandb_dict = metrics.pop()
                        wandb_dict['time'] = timer.hit()
                        wandb_dict['lr'] = self.opt.param_groups[0]['lr']
                        timer.reset()

                        # Sampling commented out for now
                        if self.total_step_counter % self.train_cfg.sample_interval == 0:
                            with ctx, torch.no_grad():
                                samples = sampler(get_ema_core(), batch, decode_fn=self.vae.decode, scale = self.train_cfg.vae_scale)
                            wandb_dict['samples'] = to_wandb(samples)

                        if self.rank == 0:
                            wandb.log(wandb_dict)

                    self.total_step_counter += 1
                    if self.total_step_counter % self.train_cfg.save_interval == 0:
                        if self.rank == 0:
                            self.save()
                        
                    self.barrier()