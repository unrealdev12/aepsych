#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
from __future__ import annotations

import time
from typing import Any, Dict, Optional

import numpy as np
import torch
from aepsych.config import Config
from aepsych.generators.base import AEPsychGenerator
from aepsych.models.base import ModelProtocol
from aepsych.utils_logging import getLogger
from botorch.acquisition import (
    AcquisitionFunction,
    LogNoisyExpectedImprovement,
    NoisyExpectedImprovement,
    qLogNoisyExpectedImprovement,
    qNoisyExpectedImprovement,
)
from botorch.acquisition.preference import AnalyticExpectedUtilityOfBestOption
from botorch.utils.sampling import draw_sobol_samples, manual_seed
from numpy.random import choice
from torch.quasirandom import SobolEngine

logger = getLogger()


class AcqfThompsonSamplerGenerator(AEPsychGenerator):
    """Generator that chooses points by minimizing an acquisition function."""

    baseline_requiring_acqfs = [
        NoisyExpectedImprovement,
        LogNoisyExpectedImprovement,
        qNoisyExpectedImprovement,
        qLogNoisyExpectedImprovement,
    ]

    def __init__(
        self,
        lb: torch.Tensor,
        ub: torch.Tensor,
        acqf: AcquisitionFunction,
        acqf_kwargs: Optional[Dict[str, Any]] = None,
        samps: int = 1000,
        stimuli_per_trial: int = 1,
    ) -> None:
        """Initialize OptimizeAcqfGenerator.
        Args:
            lb (torch.Tensor): Lower bounds for the optimization.
            ub (torch.Tensor): Upper bounds for the optimization.
            acqf (AcquisitionFunction): Acquisition function to use.
            acqf_kwargs (Dict[str, object], optional): Extra arguments to
                pass to acquisition function. Defaults to no arguments.
            samps (int): Number of samples for quasi-random initialization of the acquisition function optimizer. Defaults to 1000.
            stimuli_per_trial (int): Number of stimuli per trial. Defaults to 1.
        """

        if acqf_kwargs is None:
            acqf_kwargs = {}
        self.acqf = acqf
        self.acqf_kwargs = acqf_kwargs
        self.samps = samps
        self.stimuli_per_trial = stimuli_per_trial
        self.lb = lb
        self.ub = ub

    def _instantiate_acquisition_fn(self, model: ModelProtocol) -> AcquisitionFunction:
        """Instantiate the acquisition function with the model and any extra arguments.

        Args:
            model (ModelProtocol): The model to use for the acquisition function.

        Returns:
            AcquisitionFunction: The instantiated acquisition function.
        """
        if self.acqf == AnalyticExpectedUtilityOfBestOption:
            return self.acqf(pref_model=model)

        if self.acqf in self.baseline_requiring_acqfs:
            return self.acqf(model, model.train_inputs[0], **self.acqf_kwargs)
        else:
            return self.acqf(model=model, **self.acqf_kwargs)

    def gen(self, num_points: int, model: ModelProtocol, **gen_options) -> torch.Tensor:
        """Query next point(s) to run by optimizing the acquisition function.
        Args:
            num_points (int): Number of points to query.
            model (ModelProtocol): Fitted model of the data.
        Returns:
            torch.Tensor: Next set of point(s) to evaluate, [num_points x dim].
        """

        if self.stimuli_per_trial == 2:
            qbatch_points = self._gen(
                num_points=num_points * 2, model=model, **gen_options
            )

            # output of super() is (q, dim) but the contract is (num_points, dim, 2)
            # so we need to split q into q and pairs and then move the pair dim to the end
            return qbatch_points.reshape(num_points, 2, -1).swapaxes(-1, -2)

        else:
            return self._gen(num_points=num_points, model=model, **gen_options)

    def _gen(
        self, num_points: int, model: ModelProtocol, **gen_options
    ) -> torch.Tensor:
        """
        Generates the next query points by optimizing the acquisition function.

        Args:
            num_points (int): The number of points to query.
            model (ModelProtocol): The fitted model used to evaluate the acquisition function.
            gen_options (dict): Additional options for generating points, including:
                - "seed": Random seed for reproducibility.

        Returns:
            torch.Tensor: Next set of points to evaluate, with shape [num_points x dim].
        """

        # eval should be inherited from superclass
        model.eval()  # type: ignore
        acqf = self._instantiate_acquisition_fn(model)

        logger.info("Starting gen...")
        starttime = time.time()

        seed = gen_options.get("seed")
        bounds = torch.tensor(np.c_[self.lb, self.ub]).T.cpu()
        bounds_cpu = bounds.cpu()
        effective_dim = bounds.shape[-1] * num_points
        if effective_dim <= SobolEngine.MAXDIM:
            X_rnd = draw_sobol_samples(
                bounds=bounds_cpu, n=self.samps, q=num_points, seed=seed
            )
        else:
            with manual_seed(seed):
                X_rnd_nlzd = torch.rand(
                    self.samps, num_points, bounds_cpu.shape[-1], dtype=bounds.dtype
                )
            X_rnd = bounds_cpu[0] + (bounds_cpu[1] - bounds_cpu[0]) * X_rnd_nlzd

        acqf_vals = acqf(X_rnd).to(torch.float64)
        acqf_vals -= acqf_vals.min()
        probability_dist = acqf_vals / acqf_vals.sum()
        candidate_idx = choice(
            np.arange(X_rnd.shape[0]), size=1, p=probability_dist.detach().numpy()
        )
        new_candidate = X_rnd[candidate_idx].squeeze(0)

        logger.info(f"Gen done, time={time.time()-starttime}")
        return new_candidate

    @classmethod
    def from_config(cls, config: Config) -> AcqfThompsonSamplerGenerator:
        """Initialize AcqfThompsonSamplerGenerator from configuration.

        Args:
            config (Config): Configuration object containing initialization parameters.

        Returns:
            AcqfThompsonSamplerGenerator: The initialized generator.
        """
        classname = cls.__name__
        lb = config.gettensor(classname, "lb")
        ub = config.gettensor(classname, "ub")
        acqf = config.getobj(classname, "acqf", fallback=None)
        extra_acqf_args = cls._get_acqf_options(acqf, config)
        stimuli_per_trial = config.getint(classname, "stimuli_per_trial")
        samps = config.getint(classname, "samps", fallback=1000)

        return cls(
            lb=lb,
            ub=ub,
            acqf=acqf,
            acqf_kwargs=extra_acqf_args,
            samps=samps,
            stimuli_per_trial=stimuli_per_trial,
        )
