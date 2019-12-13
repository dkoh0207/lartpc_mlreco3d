import numpy as np
import pandas as pd
import sys
import os, re
import torch
import yaml
import time
from scipy.spatial.distance import cdist
from sklearn.metrics import adjusted_rand_score as ari
from pathlib import Path
import argparse

current_directory = os.path.dirname(os.path.abspath(__file__))
current_directory = os.path.dirname(current_directory)
sys.path.insert(0, current_directory)

from mlreco.main_funcs import process_config, train, inference
from mlreco.utils.metrics import *
from mlreco.trainval import trainval
from mlreco.main_funcs import process_config
from mlreco.iotools.factories import loader_factory
from mlreco.main_funcs import cycle


def make_inference_cfg(train_cfg, gpu=1, snapshot=None, batch_size=1, model_path=None):

    cfg = yaml.load(open(train_cfg, 'r'), Loader=yaml.Loader)
    process_config(cfg)
    inference_cfg = cfg.copy()
    data_keys = inference_cfg['iotool']['dataset']['data_keys']
    
    # Change dataset to validation samples
    data_val = []
    for file_path in data_keys:
        data_val.append(file_path.replace('train', 'test'))
    inference_cfg['iotool']['dataset']['data_keys'] = data_val
    
    # Change batch size to 1 since no need for batching during validation
    inference_cfg['iotool']['batch_size'] = batch_size
    inference_cfg['iotool'].pop('sampler', None)
    inference_cfg['iotool'].pop('minibatch_size', None)
    inference_cfg['trainval']['gpus'] = str(gpu)
    inference_cfg['trainval']["train"] = False
    
    # Analysis keys for clustering
    inference_cfg['model']["analysis_keys"] = {
        "segmentation": 0,
        "clustering": 1,
    }
    
    # Get latest model path if checkpoint not provided.
    if model_path is None:
        model_path = inference_cfg['trainval']['model_path']
    else:
        inference_cfg['trainval']['model_path'] = model_path
    if snapshot is None:
        checkpoints = [int(re.findall('snapshot-([0-9]+).ckpt', f)[0]) for f in os.listdir(
            re.sub(r'snapshot-([0-9]+).ckpt', '', model_path)) if 'snapshot' in f]
        print(checkpoints)
        latest_ckpt = max(checkpoints)
        model_path = re.sub(r'snapshot-([0-9]+)', 'snapshot-{}'.format(str(latest_ckpt)), model_path)
    else:
        model_path = re.sub(r'snapshot-([0-9]+)', 'snapshot-{}'.format(snapshot), model_path)
    inference_cfg['trainval']['model_path'] = model_path
    process_config(inference_cfg)
    return inference_cfg


def gaussian_kernel(centroid, sigma):
    def f(x):
        dists = np.sum(np.power(x - centroid, 2), axis=1, keepdims=False)
        probs = np.exp(-dists / (2.0 * sigma**2))
        return probs
    return f


def ellipsoidal_kernel(centroid, sigma):
    def f(x):
        dists = np.power(x - centroid, 2) / (2.0 * sigma**2)
        probs = np.exp(-np.sum(-dists, axis=1, keepdims=False))
        return probs
    return f


def find_cluster_means(features, labels):
    '''
    For a given image, compute the centroids \mu_c for each
    cluster label in the embedding space.

    INPUTS:
        features (torch.Tensor) - the pixel embeddings, shape=(N, d) where
        N is the number of pixels and d is the embedding space dimension.

        labels (torch.Tensor) - ground-truth group labels, shape=(N, )

    OUTPUT:
        cluster_means (torch.Tensor) - (n_c, d) tensor where n_c is the number of
        distinct instances. Each row is a (1,d) vector corresponding to
        the coordinates of the i-th centroid.
    '''
    group_ids = sorted(np.unique(labels).astype(int))
    cluster_means = []
    #print(group_ids)
    for c in group_ids:
        index = labels.astype(int) == c
        mu_c = features[index].mean(0)
        cluster_means.append(mu_c)
    cluster_means = np.vstack(cluster_means)
    return group_ids, cluster_means


def cluster_remainder(embedding, semi_predictions):
    if sum(semi_predictions == -1) == 0 or sum(semi_predictions != -1) == 0:
        return semi_predictions
    group_ids, predicted_cmeans = find_cluster_means(
        embedding, semi_predictions)
    semi_predictions[semi_predictions == -1] = np.argmin(
        cdist(embedding[semi_predictions == -1], predicted_cmeans[1:]), axis=1)
    return semi_predictions


def fit_predict1(embeddings, seediness, margins, threshold=0.9, cluster_all=False):
    pred_labels = -np.ones(embeddings.shape[0])
    spheres = []
    seediness_copy = np.copy(seediness)
    check_completion = np.zeros(seediness.shape[0], dtype=int)
    c = 0
    while not check_completion.all():
        i = np.argsort(seediness_copy)[::-1][0]
        seedScore = seediness[i]
        if seedScore < s_threshold:
            break
        centroid = embeddings[i]
        sigma = margins[i]
        spheres.append((centroid, sigma))
        f = fitfunc(centroid, sigma)
        pValues = f(embeddings)
        cluster_index = pValues > p_threshold
        seediness_copy[cluster_index] = 0
        check_completion[cluster_index] = 1
        pred_labels[cluster_index] = c
        c += 1
    if cluster_all:
        pred_labels = cluster_remainder(embeddings, pred_labels)
    return pred_labels, spheres


def fit_predict2(embeddings, seediness, margins, fitfunc, 
                 s_threshold=0.0, p_threshold=0.5, cluster_all=False):
    pred_labels = -np.ones(embeddings.shape[0])
    probs = []
    spheres = []
    seediness_copy = np.copy(seediness)
    count = 0
    while count < seediness.shape[0]:
        i = np.argsort(seediness_copy)[::-1][0]
        seedScore = seediness[i]
        if seedScore < s_threshold:
            break
        centroid = embeddings[i]
        sigma = margins[i]
        spheres.append((centroid, sigma))
        f = fitfunc(centroid, sigma)
        pValues = f(embeddings)
        probs.append(pValues.reshape(-1, 1))
        cluster_index = pValues > p_threshold
        seediness_copy[cluster_index] = 0
        count += sum(cluster_index)
    if len(probs) == 0:
        return pred_labels, spheres
    probs = np.hstack(probs)
    pred_labels = np.argmax(probs, axis=1)
    # if cluster_all:
    #     pred_labels = cluster_remainder(embeddings, pred_labels)
    return pred_labels, spheres


def main_loop(train_cfg, **kwargs):

    inference_cfg = make_inference_cfg(train_cfg, gpu=kwargs['gpu'], batch_size=1,
                        model_path=kwargs['model_path'])
    start_index = kwargs.get('start_index', 0)
    end_index = kwargs.get('end_index', 20000)
    event_list = list(range(start_index, end_index))
    loader = loader_factory(inference_cfg, event_list=event_list)
    dataset = iter(cycle(loader))
    Trainer = trainval(inference_cfg)
    loaded_iteration = Trainer.initialize()
    output = []

    inference_cfg['trainval']['iterations'] = len(event_list)
    iterations = inference_cfg['trainval']['iterations']
    # s_threshold = kwargs['s_threshold']
    # p_threshold = kwargs['p_threshold']
    s_thresholds = {0: 0.88, 1: 0.92, 2: 0.84, 3: 0.84, 4: 0.8}
    p_thresholds = {0: 0.5, 1: 0.5, 2: 0.5, 3: 0.5, 4: 0.5}

    for i in event_list:

        print("Iteration: %d" % i)

        data_blob, res = Trainer.forward(dataset)
        # segmentation = res['segmentation'][0]
        embedding = res['embeddings'][0]
        seediness = res['seediness'][0].reshape(-1, )
        margins = res['margins'][0].reshape(-1, )
        semantic_labels = data_blob['segment_label'][0][:, -1]
        cluster_labels = data_blob['cluster_label'][0][:, -1]
        coords = data_blob['input_data'][0][:, :3]
        index = data_blob['index'][0]

        acc_dict = {}

        for c in (np.unique(semantic_labels)):
            semantic_mask = semantic_labels == c
            clabels = cluster_labels[semantic_mask]
            embedding_class = embedding[semantic_mask]
            coords_class = coords[semantic_mask]
            seed_class = seediness[semantic_mask]
            margins_class = margins[semantic_mask]
            print(index, c)
            pred, spheres = fit_predict2(embedding_class, seed_class, margins_class, gaussian_kernel,
                                s_threshold=s_thresholds[int(c)], p_threshold=p_thresholds[int(c)], cluster_all=True)
            purity, efficiency = purity_efficiency(pred, clabels)
            fscore = 2 * (purity * efficiency) / (purity + efficiency)
            ari = ARI(pred, clabels)
            nclusters = len(np.unique(clabels))
            _, true_centroids = find_cluster_means(coords_class, clabels)
            for j, cluster_id in enumerate(np.unique(clabels)):
                margin = np.mean(margins_class[clabels == cluster_id])
                true_size = np.std(np.linalg.norm(coords_class[clabels == cluster_id] - true_centroids[j], axis=1))
                row = (index, c, ari, purity, efficiency, fscore, \
                    nclusters, s_thresholds[int(c)], p_thresholds[int(c)], margin, true_size)
                output.append(row)
            print("ARI = ", ari)

    output = pd.DataFrame(output, columns=['Index', 'Class', 'ARI',
                'Purity', 'Efficiency', 'FScore', 'num_clusters', 
                'seed_threshold', 'prob_threshold', 'margin', 'true_size'])
    return output


if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    parser.add_argument('-test_cfg', '--test_config', help='config_path', type=str)
    #parser.add_argument('-ckpt', '--checkpoint_number', type=int)
    args = parser.parse_args()
    args = vars(args)
    cfg = yaml.load(open(args['test_config'], 'r'), Loader=yaml.Loader)

    train_cfg = cfg['config_path']
    s_thresholds = [0.0]
    p_thresholds = [0.5]
    for p in p_thresholds:
        for t in s_thresholds:
            start = time.time()
            output = main_loop(train_cfg, s_threshold=t, p_threshold=p, **cfg)
            end = time.time()
            print("Time = {}".format(end - start))
            name = '{}.csv'.format(cfg['name'])
            if not os.path.exists(cfg['target']):
                os.mkdir(cfg['target'])
            target = os.path.join(cfg['target'], name)
            output.to_csv(target, index=False, mode='a', chunksize=50)