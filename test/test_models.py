from __future__ import print_function
from __future__ import absolute_import
from __future__ import division
import pytest
from mlreco.models import factories
from mlreco.trainval import trainval
import numpy as np
import torch
import os
os.environ['CUDA_VISIBLE_DEVICES']=''

@pytest.fixture(params=factories.model_dict().keys())
def config(request):
    """
    Fixture to generate a basic configuration dictionary given a model name.
    """
    model_name = request.param
    model, criterion = factories.construct(model_name)
    if 'chain' in model_name:
        model_config = {
            'name': model_name,
            'modules': {}
        }
        for module in model.MODULES:
            model_config['modules'][module] = {}
    else:
        model_config = {
            'name': model_name,
            'modules': {
                model_name: {}
            }
        }
    model_config['network_input'] = ['input_data', 'segment_label']
    model_config['loss_input'] = ['segment_label']
    iotool_config = {
        'batch_size': 1,
        'minibatch_size': 1,
    }
    config = {
        'iotool': iotool_config,
        'training': {'gpus': ''},
        'model': model_config
    }
    return config


def test_model_construction(config):
    """
    Tests whether a model and its loss can be constructed.
    """
    model, criterion = factories.construct(config['model']['name'])
    net = model(config['model'])
    loss = criterion(config['model'])

    net.eval()
    net.train()


parsers = {
    "parse_sparse3d_scn": (3, 1),
    "parse_sparse3d": (0, 3+1),
    "parse_tensor3d": (0, 0),  # TODO
    "parse_particle_points": (3, 1),
    "parse_particle_infos": (3, 6),
    "parse_em_primaries": (6, 1),
    "parse_dbscan": (3, 1),
    "parse_dbscan_groups": (3, 1),
    "parse_cluster3d": (3, 1),
    "parse_sparse3d_clean": (3, 3),
    "parse_cluster3d_clean": (3, 1),
    "parse_cluster3d_scales": [(3, 1)]*5,
    "parse_sparse3d_scn_scales": [(3, 1)]*5
}


def test_model_train(config):
    """
    Test whether a model can be trained.
    Using only numpy input arrays, should also test with parsers running.
    """
    model, criterion = factories.construct(config['model']['name'])
    net = model(config['model'])
    loss = criterion(config['model'])

    if not hasattr(net, "INPUT_SCHEMA"):
        pytest.skip('No test defined for network of %s' % config['model']['name'])

    net_input, voxels = generate_data(net.INPUT_SCHEMA)
    output = net.forward(net_input)

    if not hasattr(loss, "INPUT_SCHEMA"):
        pytest.skip('No test defined for criterion of %s' % config['model']['name'])

    loss_input = ([[x[0]] for x in output],) + generate_data(loss.INPUT_SCHEMA, voxels=voxels, loss=True)[0]
    res = loss.forward(*loss_input)

    res['loss'].backward()


def generate_data(input_schema, voxels=None, loss=False):
    N = 192
    net_input = ()
    num_voxels = np.random.randint(low=20, high=100)
    original_voxels = voxels
    for schema in input_schema:
        obj = None
        shapes = parsers[schema[0]]
        types = schema[1]
        # TODO downsamples here, define voxels once only and downsample coordinates
        if isinstance(shapes, list):
            out = []
            if original_voxels is None:
                original_voxels = np.random.random((num_voxels, shapes[0][0])) * N

            voxels = original_voxels
            values = []
            for t in types:
                values.append(np.random.random((voxels.shape[0], shapes[0][1])).astype(t))
            out.append(np.concatenate([voxels, np.zeros((voxels.shape[0], 1))] + values, axis=1))

            for shape in shapes[1:]:
                voxels = np.floor(voxels/float(2))
                voxels, indices = np.unique(voxels, axis=0, return_index=True)
                for i in range(len(values)):
                    values[i] = values[i][indices]
                out.append(np.concatenate([voxels, np.zeros((voxels.shape[0], 1))] + values, axis=1))
            obj = out
            net_input += ([torch.tensor(x) for x in obj],) if not loss else ([[torch.tensor(x) for x in obj]],)
        elif isinstance(shapes, tuple):
            if original_voxels is None:
                original_voxels = np.random.random((num_voxels, shapes[0])) * N

            voxels = original_voxels
            values = []
            for t in types:
                values.append(np.random.random((voxels.shape[0], shapes[1])).astype(t))
            obj = np.concatenate([voxels, np.zeros((voxels.shape[0], 1))] + values, axis=1)
            net_input += (torch.tensor(obj),) if not loss else ([torch.tensor(obj)],)

    return net_input, original_voxels
