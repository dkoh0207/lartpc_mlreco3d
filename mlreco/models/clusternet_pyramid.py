import torch
import sys
import torch.nn as nn
import numpy as np
import torch.nn.functional as F
import torch.optim as optim
import sparseconvnet as scn
from collections import defaultdict

from mlreco.models.discriminative_loss import DiscriminativeLoss
from mlreco.models.clusternet import ClusterNet 

class PyramidNet(torch.nn.Module):
    """
    UResNet

    For semantic segmentation, using sparse convolutions from SCN, but not the
    ready-made UNet from SCN library. The option `ghost` allows to train at the
    same time for semantic segmentation between N classes (e.g. particle types)
    and ghost points masking.

    Can also be used in a chain, for example stacking PPN layers on top.

    Configuration
    -------------
    num_strides : int
        Depth of UResNet, also corresponds to how many times we down/upsample.
    filters : int
        Number of filters in the first convolution of UResNet.
        Will increase linearly with depth.
    num_classes : int
        Should be number of classes (+1 if we include ghost points directly)
    data_dim : int
        Dimension 2 or 3
    spatial_size : int
        Size of the cube containing the data, e.g. 192, 512 or 768px.
    ghost : bool, optional
        Whether to compute ghost mask separately or not. See SegmentationLoss
        for more details.
    reps : int, optional
        Convolution block repetition factor
    kernel_size : int, optional
        Kernel size for the SC (sparse convolutions for down/upsample).
    features: int, optional
        How many features are given to the network initially.

    Returns
    -------
    In order:
    - segmentation scores (N, 5)
    - feature map for PPN1
    - feature map for PPN2
    - if `ghost`, segmentation scores for deghosting (N, 2)
    """

    def __init__(self, cfg, name="clusternet"):
        super(PyramidNet, self).__init__()
        import sparseconvnet as scn
        self._model_config = cfg['modules'][name]

        # Whether to compute ghost mask separately or not
        self._dimension = self._model_config.get('data_dim', 3)
        reps = self._model_config.get('reps', 2)  # Conv block repetition factor
        kernel_size = self._model_config.get('kernel_size', 2)
        num_strides = self._model_config.get('num_strides', 5)
        self._num_strides = num_strides
        m = self._model_config.get('filters', 16)  # Unet number of features
        nInputFeatures = self._model_config.get('features', 1)
        self.spatial_size = self._model_config.get('spatial_size', 512)
        num_classes = self._model_config.get('num_classes', 5)
        self._N = self._model_config.get('N', 0)
        self._coordConv = self._model_config.get('coordConv', False)
        self._simpleN = self._model_config.get('simple_conv', False)
        self._hypDim = self._model_config.get('hypDim', 16)

        nPlanes = [i*m for i in range(1, num_strides+1)]  # UNet number of features per level
        fcsize = sum(nPlanes)
        nPlanes_decoder = [i * int(fcsize / num_strides) for i in range(num_strides, 0, -1)]
        downsample = [kernel_size, 2]  # [filter size, filter stride]
        self.last = None
        leakiness = 0.0

        # Clusternet Backbone (Multilayer Loss)
        self.clusternet = ClusterNet(cfg, name='clusternet')

        # UnPooling
        self.unpooling = scn.Sequential()

        for i in range(num_strides):
            if i < num_strides-1:
                module_unpool = scn.Sequential()
                for _ in range(num_strides-1-i):
                    module_unpool.add(
                        scn.UnPooling(self._dimension, downsample[0], downsample[1]))
                self.unpooling.add(module_unpool)

        # Feature Reducing Layers (NINs)
        self.cluster_decoder = scn.Sequential()
        for i in range(num_strides-1):
            module = scn.Sequential()
            module.add(
                scn.BatchNormLeakyReLU(nPlanes_decoder[i])).add(
                scn.NetworkInNetwork(nPlanes_decoder[i], nPlanes_decoder[i+1], False))
            self.cluster_decoder.add(module)

        # Output Layer for Clustering
        self.cluster_output = scn.Sequential().add(
           scn.BatchNormReLU(nPlanes_decoder[-1])).add(
           scn.OutputLayer(self._dimension))

        self.concat = scn.JoinTable()

        # Last Linear Layers (for tunable hyperspace dimension)
        self.linear = torch.nn.Linear(nPlanes_decoder[-1] + (3 if self._coordConv else 0), self._hypDim)

    def forward(self, input):
        """
        point_cloud is a list of length minibatch size (assumes mbs = 1)
        point_cloud[0] has 3 spatial coordinates + 1 batch coordinate + 1 feature
        label has shape (point_cloud.shape[0] + 5*num_labels, 1)
        label contains segmentation labels for each point + coords of gt points
        """
        point_cloud = input[0]
        coords = point_cloud[:, 0:self._dimension+1].float()
        features = point_cloud[:, self._dimension+1:self._dimension+2].float()

        cnet_output = self.clusternet(input)

        feature_dec = cnet_output['cluster_feature']

        # Feature Maps for Clustering
        feature_clustering = []
        for i, layer in enumerate(feature_dec[0][::-1]):
            if i < self._num_strides-1:
                f = self.unpooling[i](layer)
                feature_clustering.append(f)
            else:
                feature_clustering.append(layer)
        
        out = self.concat(feature_clustering)
        out = self.cluster_decoder(out)
        out = self.cluster_output(out)
        if self._coordConv:
            normalized_coords = (coords[:, 0:3] - self.spatial_size /2) / float(self.spatial_size)
            out = torch.cat([out, normalized_coords], dim=1)
        out = self.linear(out)

        res = {
            'segmentation': cnet_output['segmentation'],
            'cluster_features': [out]
            }

        return res


class ClusteringLoss(nn.Module):

    def __init__(self, cfg, reduction='sum'):
        super(ClusteringLoss, self).__init__()
        self._cfg = cfg['modules']['discriminative_loss']
        self.clustering_loss = DiscriminativeLoss(cfg)
    
    def forward(self, result, slabel, clabel):
        cluster_res = self.clustering_loss(result, slabel, clabel)
        return cluster_res