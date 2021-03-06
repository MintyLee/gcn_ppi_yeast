import time
import unittest

import numpy as np
import scipy.sparse as sp
import tensorflow as tf
from sklearn.metrics import roc_auc_score
from sklearn.metrics import average_precision_score
import networkx as nx

from gcn.utils import load_data, sparse_to_tuple, weight_variable_glorot, dropout_sparse, sym_normalize_matrix

# Set random seed
seed = 123
np.random.seed(seed)
tf.set_random_seed(seed)

# Settings
flags = tf.app.flags
FLAGS = flags.FLAGS
flags.DEFINE_float('learning_rate', 0.01, 'Initial learning rate.')
flags.DEFINE_integer('epochs', 20, 'Number of epochs to train.')
flags.DEFINE_integer('hidden1', 32, 'Number of units in hidden layer 1.')
flags.DEFINE_integer('hidden2', 16, 'Number of units in hidden layer 2.')
flags.DEFINE_float('dropout', 0.1, 'Dropout rate (1 - keep probability).')
flags.DEFINE_boolean('regenerate_training_data', False, 'Flag to indicate whether or not to '
                     'regenerate training/val/test data. Default: load precalculated datasets')


def construct_feed_dict(adj_normalized, adj, features, placeholders):
    feed_dict = dict()
    feed_dict.update({placeholders['features']: features})
    feed_dict.update({placeholders['adj']: adj_normalized})
    feed_dict.update({placeholders['adj_orig']: adj})
    return feed_dict


def get_roc_score(edges_pos, edges_neg):
    feed_dict.update({placeholders['dropout']: 0})
    emb = sess.run(model.embeddings, feed_dict=feed_dict)

    def sigmoid(x):
        return 1 / (1 + np.exp(-x))

    # Predict on test set of edges
    adj_rec = np.dot(emb, emb.T)
    preds = []
    pos = []
    for e in edges_pos:
        preds.append(sigmoid(adj_rec[e[0], e[1]]))
        pos.append(adj_orig[e[0], e[1]])

    preds_neg = []
    neg = []
    for e in edges_neg:
        preds_neg.append(sigmoid(adj_rec[e[0], e[1]]))
        neg.append(adj_orig[e[0], e[1]])

    preds_all = np.hstack([preds, preds_neg])
    labels_all = np.hstack([np.ones(len(preds)), np.zeros(len(preds))])
    roc_score = roc_auc_score(labels_all, preds_all)
    ap_score = average_precision_score(labels_all, preds_all)

    return roc_score, ap_score


class GraphConvolutionSparse():
    """Graph convolution layer for sparse inputs."""

    def __init__(self, input_dim, output_dim, adj, features_nonzero, name, dropout=0., act=tf.nn.relu, dtype=tf.float32):
        self.name = name
        self.dtype = dtype
        self.vars = {}
        self.issparse = False
        with tf.variable_scope(self.name + '_vars'):
            self.vars['weights'] = weight_variable_glorot(input_dim, output_dim, name='weights')
        self.dropout = dropout
        self.adj = adj
        self.act = act
        self.issparse = True
        self.features_nonzero = features_nonzero

    def __call__(self, inputs):
        with tf.name_scope(self.name):
            x = inputs
            x = dropout_sparse(x, 1 - self.dropout, self.features_nonzero)
            x = tf.sparse_tensor_dense_matmul(x, self.vars['weights'])
            x = tf.sparse_tensor_dense_matmul(self.adj, x)
            outputs = self.act(x)
            tf.debugging.check_numerics(x, "Output of layer " + str(self.name) + " has numerical instability")
        return outputs

    def set_weights(self, weights):
        with tf.variable_scope(self.name + '_vars'):
            self.vars['weights'] = weights


class GraphConvolution():
    """Basic graph convolution layer for undirected graph without edge labels."""

    def __init__(self, input_dim, output_dim, adj, name, dropout=0., act=tf.nn.relu):
        self.name = name
        self.vars = {}
        self.issparse = False
        with tf.variable_scope(self.name + '_vars'):
            self.vars['weights'] = weight_variable_glorot(input_dim, output_dim, name='weights')
        self.dropout = dropout
        self.adj = adj
        self.act = act

    def __call__(self, inputs):
        with tf.name_scope(self.name):
            x = inputs
            x = tf.nn.dropout(x, 1 - self.dropout)
            x = tf.matmul(x, self.vars['weights'])
            x = tf.sparse_tensor_dense_matmul(self.adj, x)
            outputs = self.act(x)
        return outputs

    def set_weights(self, weights):
        with tf.variable_scope(self.name + '_vars'):
            self.vars['weights'] = weights


class InnerProductDecoder():
    """Decoder model layer for link prediction."""

    def __init__(self, name, dropout=0., act=tf.nn.sigmoid):
        self.name = name
        self.issparse = False
        self.dropout = dropout
        self.act = act

    def __call__(self, inputs):
        with tf.name_scope(self.name):
            inputs = tf.nn.dropout(inputs, 1 - self.dropout)
            x = tf.transpose(inputs)
            x = tf.matmul(inputs, x)
            x = tf.reshape(x, [-1])
            outputs = self.act(x)
        return outputs


# ------------------------------------------------------------------------------
# Like good engineers, let's validate our code works!
# ------------------------------------------------------------------------------

class TestLayer(unittest.TestCase):
    def test_propagate_node_state(self):
        sparse_adj = tf.SparseTensor([(0, 1), (1, 0)], np.array([1.0, 1.0], np.float32), (3, 3))
        gcs = GraphConvolutionSparse(3, 3, sparse_adj, 3, 'gcn_sparse_layer')
        weights = tf.constant([
            [1.0, 0.0],
            [0.0, 1.0],
            [0.0, 0.0]
        ])
        gcs.set_weights(weights)
        # Create an identity matrix below as the feature matrix (node_state) X
        node_state = tf.SparseTensor([(0, 0), (1, 1), (2, 2)], np.array([1.0, 1.0, 1.0], np.float32), (3, 3))
        result = gcs(node_state)
        expected_result = [
            [[0.0, 1.0],
            [1.0, 0.0],
            [0.0, 0.0]]
        ]
        expected_result = tf.convert_to_tensor(expected_result, dtype=tf.float32)
        # np.testing.assert_allclose(result, np.asanyarray(expected_result, dtype=np.float32), rtol=1e-03)
        tf.math.equal(result, expected_result)
        print("test_propagate_node_state Success!")

    def test_apply_convolution(self):
        sparse_adj = tf.SparseTensor([(0, 1), (1, 0)], np.array([1.0, 1.0], np.float32), (3, 3))
        gcs = GraphConvolution(3, 3, sparse_adj, 'gcn_dense_layer')
        weights = tf.constant([
            [0.0, 1.0],
            [1.0, 0.0],
        ])
        gcs.set_weights(weights)
        # Create an identity matrix below as the feature matrix (node_state) X
        node_state = tf.constant([
            [1.0, 0.0],
            [1.0, 0.0],
            [0.0, 0.0]
        ], tf.float32)
        node_state = tf.convert_to_tensor(node_state, dtype=tf.float32)
        result = gcs(node_state)
        expected_result = [
             [0.0, 1.0],
             [0.0, 1.0],
             [0.0, 0.0]
        ]
        expected_result = tf.convert_to_tensor(expected_result, dtype=tf.float32)
        # np.testing.assert_allclose(result, np.asanyarray(expected_result, dtype=np.float32), rtol=1e-03)
        tf.math.equal(result, expected_result)

        print("test_apply_convolution Success!")

    def test_decoder(self):
        embeddings = [
            [0.0, 1.0],
            [0.0, 1.0],
            [0.0, 0.0]
        ]
        embeddings = tf.convert_to_tensor(embeddings, dtype=tf.float32)
        decoder = InnerProductDecoder( name='gcn_decoder', act=lambda x: x)
        result = decoder(embeddings)
        expected_result = np.array([1., 1., 0., 1., 1., 0., 0., 0., 0.], dtype=np.float32)
        tf.convert_to_tensor(expected_result, dtype=tf.float32)
        tf.math.equal(result, expected_result)
        print("test_decoder_Success!")


t = TestLayer()
t.test_propagate_node_state()
t.test_apply_convolution()
t.test_decoder()


class GCNModel():
    def __init__(self, placeholders, num_features, features_nonzero, name):
        self.name = name
        self.inputs = placeholders['features']
        self.input_dim = num_features
        self.features_nonzero = features_nonzero
        self.adj = placeholders['adj']
        self.dropout = placeholders['dropout']
        with tf.variable_scope(self.name):
            self.build()

    def build(self):
        self.hidden1 = GraphConvolutionSparse(
            name='gcn_sparse_layer',
            input_dim=self.input_dim,
            output_dim=FLAGS.hidden1,
            adj=self.adj,
            features_nonzero=self.features_nonzero,
            act=tf.nn.relu,
            dropout=self.dropout)(self.inputs)

        self.embeddings = GraphConvolution(
            name='gcn_dense_layer',
            input_dim=FLAGS.hidden1,
            output_dim=FLAGS.hidden2,
            adj=self.adj,
            act=lambda x: x,
            dropout=self.dropout)(self.hidden1)

        self.reconstructions = InnerProductDecoder(
            name='gcn_decoder',
            act=lambda x: x)(self.embeddings)


class Optimizer():
    def __init__(self, preds, labels, num_nodes, num_edges):
        pos_weight = float(num_nodes ** 2 - num_edges) / num_edges
        norm = num_nodes ** 2 / float((num_nodes ** 2 - num_edges) * 2)

        preds_sub = preds
        labels_sub = labels

        self.cost = norm * tf.reduce_mean(
            tf.nn.weighted_cross_entropy_with_logits(
                logits=preds_sub, targets=labels_sub, pos_weight=pos_weight))
        self.optimizer = tf.train.AdamOptimizer(learning_rate=FLAGS.learning_rate)  # Adam Optimizer

        self.opt_op = self.optimizer.minimize(self.cost)
        self.grads_vars = self.optimizer.compute_gradients(self.cost)


# Given a training set of protein-protein interactions in yeast S. cerevisiae, our goal is to take these interactions
# and train a GCN model that can predict new protein-protein interactions. That is, we would like to predict new
# edges in the yeast protein interaction network.
print("Start")
# Check if regenerate_training_date is set to True: regenerate training/validation/test data
adj, adj_train, val_edges, val_edges_false, test_edges, test_edges_false = load_data()

num_nodes = adj.shape[0]
num_edges = adj.sum()

#
# Simple GCN: no node features (featureless). Substitute the identity matrix for the feature matrix: X = I
#
features = sparse_to_tuple(sp.identity(num_nodes))
num_features = features[2][1]
features_nonzero = features[1].shape[0]

#
# Store original adjacency matrix (without diagonal entries) for later
#
adj_orig = (adj - sp.dia_matrix((adj.diagonal()[np.newaxis, :], [0]), shape=adj.shape))
adj_orig.eliminate_zeros()

adj_norm = sparse_to_tuple(sym_normalize_matrix(adj_train + sp.eye(adj.shape[0])))

# Since the adj_train matrix was not created with diagonal entires, add them now.
adj_label = sparse_to_tuple(adj_train + sp.eye(adj_train.shape[0]))

# Define placeholders
placeholders = {
    'features': tf.sparse_placeholder(tf.float32),
    'adj': tf.sparse_placeholder(tf.float32),
    'adj_orig': tf.sparse_placeholder(tf.float32),
    'dropout': tf.placeholder_with_default(0., shape=())
}

# Create model
model = GCNModel(placeholders, num_features, features_nonzero, name='yeast_gcn')

# Create optimizer
with tf.name_scope('optimizer'):
    opt = Optimizer(
        preds=model.reconstructions,
        labels=tf.reshape(tf.sparse_tensor_to_dense(placeholders['adj_orig'], validate_indices=False), [-1]),
        num_nodes=num_nodes,
        num_edges=num_edges)
print("Finished creating optimizer")

# Initialize session
print("Start session")
sess = tf.Session()
sess.run(tf.global_variables_initializer())

# Construct feed dictionary
feed_dict = construct_feed_dict(adj_norm, adj_label, features, placeholders)
# Train model
for epoch in range(FLAGS.epochs):
    t = time.time()
    feed_dict.update({placeholders['dropout']: FLAGS.dropout})
    # One update of parameter matrices
    _, avg_cost = sess.run([opt.opt_op, opt.cost], feed_dict=feed_dict)
    # Performance on validation set
    roc_curr, ap_curr = get_roc_score(val_edges, val_edges_false)

    print("Epoch:", '%04d' % (epoch + 1),
          "train_loss=", "{:.5f}".format(avg_cost),
          "val_roc=", "{:.5f}".format(roc_curr),
          "val_ap=", "{:.5f}".format(ap_curr),
          "time=", "{:.5f}".format(time.time() - t))

print('Optimization Finished!')

roc_score, ap_score = get_roc_score(test_edges, test_edges_false)
print('Test ROC score: {:.5f}'.format(roc_score))
print('Test AP score: {:.5f}'.format(ap_score))
