# SPDX-FileCopyrightText: Copyright (c) 2023 - 2024 NVIDIA CORPORATION & AFFILIATES.
# SPDX-FileCopyrightText: All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import sys
import torch
import matplotlib.pyplot as plt

from modulus.datapipes.climate import ERA5HDF5Datapipe

import hydra
import wandb
from hydra.utils import to_absolute_path
from omegaconf import DictConfig


class Validation:
    """Run validation on GraphCast model"""

    def __init__(self, cfg: DictConfig, model, dtype, dist):
        self.val_dir = to_absolute_path(cfg.val_dir)
        self.model = model
        self.dtype = dtype
        self.dist = dist
        interpolation_shape = (
            cfg.latlon_res if cfg.latlon_res != (721, 1440) else None
        )  # interpolate if not in native resolution
        self.val_datapipe = ERA5HDF5Datapipe(
            data_dir=os.path.join(cfg.dataset_path, "test"),
            stats_dir=os.path.join(cfg.dataset_path, "stats"),
            channels=[i for i in range(cfg.num_channels_climate)],
            interpolation_shape=interpolation_shape,
            num_steps=cfg.num_val_steps,
            batch_size=1,
            num_samples_per_year=cfg.num_val_spy,
            shuffle=False,
            device=self.dist.device,
            process_rank=self.dist.rank,
            world_size=self.dist.world_size,
            num_workers=cfg.num_workers,
        )
        print(f"Loaded validation datapipe of size {len(self.val_datapipe)}")

    @torch.no_grad()
    def step(self, channels=[0, 1, 2], iter=0):
        torch.cuda.nvtx.range_push("Validation")
        os.makedirs(self.val_dir, exist_ok=True)
        loss_epoch = 0
        for i, data in enumerate(self.val_datapipe):
            invar = data[0]["invar"].to(dtype=self.dtype)
            outvar = (
                data[0]["outvar"][0].to(dtype=self.dtype).to(device=self.dist.device)
            )

            pred = (
                torch.empty(outvar.shape)
                .to(dtype=self.dtype)
                .to(device=self.dist.device)
            )
            for t in range(outvar.shape[0]):
                outpred = self.model(invar)
                pred[t] = outpred
                invar = outpred

            loss_epoch += torch.mean(torch.pow(pred - outvar, 2))
            torch.cuda.nvtx.range_pop()

            pred = pred.to(torch.float32).cpu().numpy()
            outvar = outvar.to(torch.float32).cpu().numpy()

            del invar, outpred
            torch.cuda.empty_cache()

            if i == 0:
                for chan in channels:
                    plt.close("all")
                    fig, ax = plt.subplots(3, pred.shape[0], figsize=(15, 5))
                    fig.subplots_adjust(hspace=0.5, wspace=0.3)

                    for t in range(outvar.shape[0]):
                        im_pred = ax[0, t].imshow(pred[t, chan], vmin=-1.5, vmax=1.5)
                        ax[0, t].set_title(f"Prediction (t={t+1})", fontsize=10)
                        fig.colorbar(
                            im_pred, ax=ax[0, t], orientation="horizontal", pad=0.4
                        )

                        im_outvar = ax[1, t].imshow(
                            outvar[t, chan], vmin=-1.5, vmax=1.5
                        )
                        ax[1, t].set_title(f"Ground Truth (t={t+1})", fontsize=10)
                        fig.colorbar(
                            im_outvar, ax=ax[1, t], orientation="horizontal", pad=0.4
                        )

                        im_diff = ax[2, t].imshow(
                            abs(pred[t, chan] - outvar[t, chan]), vmin=0.0, vmax=0.5
                        )
                        ax[2, t].set_title(f"Abs. Diff. (t={t+1})", fontsize=10)
                        fig.colorbar(
                            im_diff, ax=ax[2, t], orientation="horizontal", pad=0.4
                        )

                    fig.savefig(
                        os.path.join(
                            self.val_dir,
                            f"era5_validation_channel{chan}_iter{iter}.png",
                        )
                    )
                    wandb.log({f"val_chan{chan}_iter{iter}": fig}, step=iter)

        return loss_epoch / len(self.val_datapipe)
