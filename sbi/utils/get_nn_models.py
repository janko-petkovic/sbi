from __future__ import annotations

from typing import Optional

import torch
from pyknos.mdn.mdn import MultivariateGaussianMDN
from pyknos.nflows import distributions as distributions_
from pyknos.nflows import flows, transforms
from pyknos.nflows.nn import nets
from pyknos.nflows.nn.nde import MixtureOfGaussiansMADE
from torch import nn, Tensor, float32, relu, tanh

import sbi.utils as utils
from sbi.utils.torchutils import create_alternating_binary_mask


def posterior_nn(
    model: str,
    prior_mean: Tensor,
    prior_std: Tensor,
    x_o_shape: torch.Size,
    embedding: Optional[nn.Module] = None,
    hidden_features: int = 50,
    mdn_num_components: int = 20,
    made_num_mixture_components: int = 10,
    made_num_blocks: int = 4,
    flow_num_transforms: int = 5,
) -> nn.Module:
    """Neural posterior density estimator

    Args:
        model: Model, one of maf / mdn / made / nsf
        prior_mean: Prior mean.
        prior_std: Prior standard deviation.
        x_o_numel: Number of elements in the a single observation.
            Used as input size to the NN.
        embedding: Embedding network
        hidden_features: For all, number of hidden features
        mdn_num_components: For MDNs only, number of components
        made_num_mixture_components: For MADEs only, number of mixture components
        made_num_blocks: For MADEs only, number of blocks
        flow_num_transforms: For flows only, number of transforms

    Returns:
        Neural network
    """

    # We need these asserts because mean and std can be defined outside, prior to user
    # input checks.
    assert (
        prior_mean.dtype == float32
    ), f"Prior mean must have dtype float32, is {prior_mean.dtype}."
    assert (
        prior_std.dtype == float32
    ), f"Prior std must have dtype float32, is {prior_std.dtype}."

    standardizing_transform = transforms.AffineTransform(
        shift=-prior_mean / prior_std, scale=1 / prior_std
    )

    theta_numel = prior_mean.numel()
    x_o_numel = x_o_shape.numel()

    if model == "mdn":
        neural_net = MultivariateGaussianMDN(
            features=theta_numel,
            context_features=x_o_numel,
            hidden_features=hidden_features,
            hidden_net=nn.Sequential(
                nn.Linear(x_o_numel, hidden_features),
                nn.ReLU(),
                nn.Dropout(p=0.0),
                nn.Linear(hidden_features, hidden_features),
                nn.ReLU(),
                nn.Linear(hidden_features, hidden_features),
                nn.ReLU(),
            ),
            num_components=mdn_num_components,
            custom_initialization=True,
        )

    elif model == "made":
        transform = standardizing_transform
        distribution = distributions_.MADEMoG(
            features=theta_numel,
            hidden_features=hidden_features,
            context_features=x_o_numel,
            num_blocks=made_num_blocks,
            num_mixture_components=made_num_mixture_components,
            use_residual_blocks=True,
            random_mask=False,
            activation=relu,
            dropout_probability=0.0,
            use_batch_norm=False,
            custom_initialization=True,
        )
        neural_net = flows.Flow(transform, distribution, embedding)

    elif model == "maf":
        transform = transforms.CompositeTransform(
            [
                transforms.CompositeTransform(
                    [
                        transforms.MaskedAffineAutoregressiveTransform(
                            features=theta_numel,
                            hidden_features=hidden_features,
                            context_features=x_o_numel,
                            num_blocks=2,
                            use_residual_blocks=False,
                            random_mask=False,
                            activation=tanh,
                            dropout_probability=0.0,
                            use_batch_norm=True,
                        ),
                        transforms.RandomPermutation(features=theta_numel),
                    ]
                )
                for _ in range(flow_num_transforms)
            ]
        )

        transform = transforms.CompositeTransform([standardizing_transform, transform,])

        distribution = distributions_.StandardNormal((theta_numel,))
        neural_net = flows.Flow(transform, distribution, embedding)

    elif model == "nsf":
        transform = transforms.CompositeTransform(
            [
                transforms.CompositeTransform(
                    [
                        transforms.PiecewiseRationalQuadraticCouplingTransform(
                            mask=create_alternating_binary_mask(
                                features=theta_numel, even=(i % 2 == 0)
                            ),
                            transform_net_create_fn=lambda in_features, out_features: nets.ResidualNet(
                                in_features=in_features,
                                out_features=out_features,
                                hidden_features=hidden_features,
                                context_features=x_o_numel,
                                num_blocks=2,
                                activation=relu,
                                dropout_probability=0.0,
                                use_batch_norm=False,
                            ),
                            num_bins=10,
                            tails="linear",
                            tail_bound=3.0,
                            apply_unconditional_transform=False,
                        ),
                        transforms.LULinear(theta_numel, identity_init=True),
                    ]
                )
                for i in range(flow_num_transforms)
            ]
        )

        transform = transforms.CompositeTransform([standardizing_transform, transform,])

        distribution = distributions_.StandardNormal((theta_numel,))
        neural_net = flows.Flow(transform, distribution, embedding)

    else:
        raise ValueError

    return neural_net


def likelihood_nn(
    model: str,
    theta_shape: torch.Size,
    x_o_shape: torch.Size,
    embedding: Optional[nn.Module] = None,
    hidden_features: int = 50,
    mdn_num_components: int = 20,
    made_num_mixture_components: int = 10,
    made_num_blocks: int = 4,
    flow_num_transforms: int = 5,
) -> nn.Module:
    """Neural likelihood density estimator

    Args:
        model: Model, one of maf / mdn / made / nsf
        theta_numel: event shape of the prior, number of parameters.
        x_o_numel: number of elements in a single data point.
        embedding: Embedding network
        hidden_features: For all, number of hidden features
        mdn_num_components: For MDNs only, number of components
        made_num_mixture_components: For MADEs only, number of mixture components
        made_num_blocks: For MADEs only, number of blocks
        flow_num_transforms: For flows only, number of transforms

    Returns:
        Neural network
    """

    theta_numel = theta_shape.numel()
    x_o_numel = x_o_shape.numel()

    if model == "mdn":
        neural_net = MultivariateGaussianMDN(
            features=x_o_numel,
            context_features=theta_numel,
            hidden_features=hidden_features,
            hidden_net=nn.Sequential(
                nn.Linear(theta_numel, hidden_features),
                nn.BatchNorm1d(hidden_features),
                nn.ReLU(),
                nn.Dropout(p=0.0),
                nn.Linear(hidden_features, hidden_features),
                nn.BatchNorm1d(hidden_features),
                nn.ReLU(),
                nn.Linear(hidden_features, hidden_features),
                nn.BatchNorm1d(hidden_features),
                nn.ReLU(),
            ),
            num_components=mdn_num_components,
            custom_initialization=True,
        )

    elif model == "made":
        neural_net = MixtureOfGaussiansMADE(
            features=x_o_numel,
            hidden_features=hidden_features,
            context_features=theta_numel,
            num_blocks=made_num_blocks,
            num_mixture_components=made_num_mixture_components,
            use_residual_blocks=True,
            random_mask=False,
            activation=relu,
            use_batch_norm=True,
            dropout_probability=0.0,
            custom_initialization=True,
        )

    elif model == "maf":
        transform = transforms.CompositeTransform(
            [
                transforms.CompositeTransform(
                    [
                        transforms.MaskedAffineAutoregressiveTransform(
                            features=x_o_numel,
                            hidden_features=hidden_features,
                            context_features=theta_numel,
                            num_blocks=2,
                            use_residual_blocks=False,
                            random_mask=False,
                            activation=tanh,
                            dropout_probability=0.0,
                            use_batch_norm=True,
                        ),
                        transforms.RandomPermutation(features=x_o_numel),
                    ]
                )
                for _ in range(flow_num_transforms)
            ]
        )
        distribution = distributions_.StandardNormal((x_o_numel,))
        neural_net = flows.Flow(transform, distribution, embedding)

    elif model == "nsf":
        transform = transforms.CompositeTransform(
            [
                transforms.CompositeTransform(
                    [
                        transforms.PiecewiseRationalQuadraticCouplingTransform(
                            mask=create_alternating_binary_mask(
                                features=x_o_numel, even=(i % 2 == 0)
                            ),
                            transform_net_create_fn=lambda in_features, out_features: nets.ResidualNet(
                                in_features=in_features,
                                out_features=out_features,
                                hidden_features=hidden_features,
                                context_features=theta_numel,
                                num_blocks=2,
                                activation=relu,
                                dropout_probability=0.0,
                                use_batch_norm=False,
                            ),
                            num_bins=10,
                            tails="linear",
                            tail_bound=3.0,
                            apply_unconditional_transform=False,
                        ),
                        transforms.LULinear(x_o_numel, identity_init=True),
                    ]
                )
                for i in range(flow_num_transforms)
            ]
        )
        distribution = distributions_.StandardNormal((x_o_numel,))
        neural_net = flows.Flow(transform, distribution)

    else:
        raise ValueError

    return neural_net


def classifier_nn(
    model, theta_shape: torch.Size, x_o_shape: torch.Size, hidden_features: int = 50,
) -> nn.Module:
    """Neural classifier

    Args:
        model: Model, one of linear / mlp / resnet
        theta_numel: event shape of the prior, number of parameters.
        x_o_numel: number of elements in a single data point.
        hidden_features: For all, number of hidden features

    Returns:
        Neural network
    """

    theta_numel = theta_shape.numel()
    x_o_numel = x_o_shape.numel()

    if model == "linear":
        neural_net = nn.Linear(theta_numel + x_o_numel, 1)

    elif model == "mlp":
        neural_net = nn.Sequential(
            nn.Linear(theta_numel + x_o_numel, hidden_features),
            nn.BatchNorm1d(hidden_features),
            nn.ReLU(),
            nn.Linear(hidden_features, hidden_features),
            nn.BatchNorm1d(hidden_features),
            nn.ReLU(),
            nn.Linear(hidden_features, 1),
        )

    elif model == "resnet":
        neural_net = nets.ResidualNet(
            in_features=theta_numel + x_o_numel,
            out_features=1,
            hidden_features=hidden_features,
            context_features=None,
            num_blocks=2,
            activation=relu,
            dropout_probability=0.0,
            use_batch_norm=False,
        )

    else:
        raise ValueError(f"'model' must be one of ['linear', 'mlp', 'resnet'].")

    return neural_net
