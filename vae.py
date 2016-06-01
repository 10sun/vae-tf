import functools
import sys

from functional import compose, partial
import matplotlib.pyplot as plt
import numpy as np
import tensorflow as tf


# TODO: prettytensor ?
ARCHITECTURE = [784, # MNIST = 28*28
                #128, # intermediate encoding
                500, 500,
                2] # latent space dims
# (and symmetrically back out again)

def composeAll(*args):
    """Util for multiple function composition"""
    # adapted from https://docs.python.org/3.1/howto/functional.html
    return partial(functools.reduce, compose)(*args)

def print_(var, name: str, first_n = 10, summarize = 5):
    """Util for debugging by printing values during training"""
    # tf.Print is identity fn with side effect of printing requested [vals]
    try:
        return tf.Print(var, [var], '{}: '.format(name), first_n=first_n,
                        summarize=summarize)
    except(TypeError):
        return tf.Print(var, var, '{}: '.format(name), first_n=first_n,
                        summarize=summarize)

class Layer():
    @staticmethod
    def wbVars(fan_in, fan_out, normal=True):
        """Helper to initialize weights and biases, via He's adaptation
        of Xavier init for ReLUs: https://arxiv.org/pdf/1502.01852v1.pdf
        (distribution defaults to truncated Normal; else Uniform)
        """
        # (int, int, bool) -> (tf.Variable, tf.Variable)
        stddev = tf.cast((2 / fan_in)**0.5, tf.float32)

        initial_w = (
            tf.truncated_normal([fan_in, fan_out], stddev=stddev) if normal else
            tf.random_uniform([fan_in, fan_out], -stddev, stddev) # (range therefore not truly stddev)
        )
        initial_b = tf.zeros([fan_out])

        return (tf.Variable(initial_w, trainable=True, name="weights"),
                tf.Variable(initial_b, trainable=True, name="biases"))


class Dense(Layer):
    def __init__(self, scope="dense_layer", size=None, dropout=1.,
                 nonlinearity=tf.identity):
        """Fully-connected layer"""
        # (str, int, float or tf.Variable, tf.op)
        assert size, "Must specify layer size (num nodes)"
        self.scope = scope
        self.size = size
        self.dropout = dropout # keep_prob
        self.nonlinearity = nonlinearity

    def __call__(self, tensor_in):
        """Dense layer currying - i.e. to appy specified layer to any input tensor"""
        # tf.Tensor -> tf.op
        with tf.name_scope(self.scope):
            w, b = Layer.wbVars(tensor_in.get_shape()[1].value, self.size)
            w = tf.nn.dropout(w, self.dropout)
            return self.nonlinearity(tf.matmul(tensor_in, w) + b)


class VAE():
    """Variational Autoencoder"""

    DEFAULTS = {
        "batch_size": 128,
        "epsilon_std": 1E-3,
        "learning_rate": 1E-4,
        "dropout": 0.9 # TODO
    }

    def __init__(self, architecture=ARCHITECTURE, d_hyperparams={},
                 save_graph_def=True):

        self.architecture = architecture

        self.architecture = architecture
        self.hyperparams = VAE.DEFAULTS.copy()
        self.hyperparams.update(**d_hyperparams)

        # handles for tensor ops to feed or fetch
        (self.x_in, self.dropout, self.z_mean, self.z_log_sigma,
         self.x_reconstructed, self.z_, self.x_reconstructed_,
         self.cost, self.global_step, self.train_op) = self._buildGraph()

        self.sesh = tf.Session()
        self.sesh.run(tf.initialize_all_variables())

        if save_graph_def:
            logger = tf.train.SummaryWriter("./log", self.sesh.graph)
            logger.flush()
            logger.close()

    @property
    def step(self):
        return self.global_step.eval(session=self.sesh)

    def _buildGraph(self):
        x_in = tf.placeholder(tf.float32, shape=[None, # enables variable batch size
                                                 self.architecture[0]], name="x")

        dropout = tf.placeholder_with_default(1., shape=[], name="dropout")

        # encoding: q(z|X)
        encoding = [Dense("encoding", hidden_size, dropout, tf.nn.elu)
                    # hidden layers reversed for fn composition s.t. list reads outer -> inner
                    for hidden_size in reversed(self.architecture[1:-1])]
        h_encoded = composeAll(encoding)(x_in)

        # latent distribution defined by parameters generated from hidden encoding
        z_mean = Dense("z_mean", self.architecture[-1], dropout)(h_encoded)
        z_log_sigma = Dense("z_log_sigma", self.architecture[-1], dropout)(h_encoded)
        z = self.sampleGaussian(z_mean, z_log_sigma)

        # decoding: p(X|z)
        # assumes symmetric hidden architecture
        decoding = [Dense("decoding", hidden_size, dropout, tf.nn.elu)
                    for hidden_size in self.architecture[1:-1]]
        # prepend final reconstruction as outermost fn
        # modeled as Bernoulli (i.e. with binary cross-entropy)
        decoding.insert(0, Dense("x_decoding", self.architecture[0], dropout, tf.nn.sigmoid))
        x_reconstructed = tf.identity(composeAll(decoding)(z), name="x_reconstructed")

        # loss
        # log likelihood / reconstruction loss
        ce_loss = VAE.crossEntropy(x_reconstructed, x_in)
        #cross_entropy = print_(cross_entropy, "ce")
        # Kullback-Leibler divergence: mismatch b/w learned latent dist and prior
        kl_loss = VAE.kullbackLeibler(z_mean, z_log_sigma)
        #kl_loss = print_(kl_loss, "kl")
        cost = tf.reduce_mean(ce_loss + kl_loss, name="cost")
        #cost = print_(cost, "cost")

        # optimization
        global_step = tf.Variable(0, trainable=False)
        with tf.name_scope("Adam_optimizer"):
            optimizer = tf.train.AdamOptimizer(self.hyperparams["learning_rate"])
            tvars = tf.trainable_variables()
            #grads, _ = tf.clip_by_global_norm(tf.gradients(cost, tvars), 5)
            #global_norm = tf.global_norm(tvars)
            grads_and_vars = optimizer.compute_gradients(cost, tvars)
            clipped = [(tf.clip_by_value(grad, -1, 1), tvar) # gradient clipping
                    for grad, tvar in grads_and_vars]
            #train_op = optimizer.apply_gradients(zip(grads, tvars))
            train_op = optimizer.apply_gradients(clipped, global_step=global_step,
                                                 name="minimize_cost")
            #train_op = (tf.train.AdamOptimizer(self.hyperparams["learning_rate"])
                                #.minimize(cost))

        # ops to directly explore latent space
        z_ = tf.placeholder(tf.float32, shape=[1, self.architecture[-1]], name="latent_in")
        x_reconstructed_ = composeAll(decoding)(z_)

        return (x_in, dropout, z_mean, z_log_sigma, x_reconstructed, z_,
                x_reconstructed_, cost, global_step, train_op)

    def sampleGaussian(self, mu, log_sigma):
        """Draw sample from Gaussian with given shape, subject to random noise epsilon"""
        with tf.name_scope("sample_gaussian"):
            epsilon = tf.random_normal(tf.shape(mu), mean=0, stddev=
                                       self.hyperparams['epsilon_std'],
                                       name="epsilon")
            return mu + epsilon * tf.exp(log_sigma)

    @staticmethod
    def crossEntropy(observed, actual, offset = 1e-12):
        with tf.name_scope("binary_cross_entropy"):
            # bound by clipping to avoid nan
            clip = functools.partial(tf.clip_by_value, clip_value_min=offset,
                                     clip_value_max=np.inf)
            return -tf.reduce_sum(actual * tf.log(clip(observed)) +
                                   (1 - actual) * tf.log(clip(1 - observed)))

    @staticmethod
    def kullbackLeibler(mu, log_sigma):
        with tf.name_scope("KL_divergence"):
            return -0.5 * tf.reduce_sum(1 + log_sigma - mu**2 - tf.exp(log_sigma))

    def encode(self, x):
        """Encoder from inputs to latent distribution parameters"""
        # np.array -> [float, float]
        feed_dict = {self.x_in: x, self.dropout: 1.}
        return self.sesh.run([self.z_mean, self.z_log_sigma], feed_dict=feed_dict)

    def decode(self, latent_pt):
        """Generative decoder from latent space to reconstructions of input space"""
        # np.array -> np.array
        feed_dict = {self.z_: latent_pt, self.dropout: 1.}
        return self.sesh.run(self.x_reconstructed_, feed_dict=feed_dict)

    def vae(self, x):
        """End-to-end autoencoder"""
        # np.array -> np.array
        return self.decode(self.sampleGaussian(*self.encode(x)))

    def train(self, X, max_iter=np.inf, max_epochs=np.inf, cross_validate=True, verbose=True):
        try:
            err_train = 0
            #err_cv = 0
            while True:
                x, labels = X.train.next_batch(self.hyperparams["batch_size"])
                feed_dict = {self.x_in: x,
                             self.dropout: self.hyperparams["dropout"]}
                fetches = [self.x_reconstructed, self.cost, self.global_step, self.train_op]
                x_reconstructed, cost, i, _ = self.sesh.run(fetches, feed_dict)

                err_train += cost

                if i%500 == 0 and verbose:
                    print("round {} --> avg cost: ".format(i), err_train / i)

                if i%2000 == 0 and verbose:
                    self.plotSubset(x, x_reconstructed, n=10, name="train")

                    if cross_validate:
                        x, labels = X.validation.next_batch(self.hyperparams["batch_size"])
                        feed_dict = {self.x_in: x}
                        fetches = [self.x_reconstructed, self.cost]
                        x_reconstructed, cost = self.sesh.run(fetches, feed_dict)

                        #err_cv += cost
                        print("round {} --> CV cost: ".format(i), cost)

                        self.plotSubset(x, x_reconstructed, n=10, name="cv")

                if i >= max_iter or X.train.epochs_completed >= max_epochs:
                    print("final cost: ", cost)
                    break

        except(KeyboardInterrupt):
            return


def test_mnist():
    from tensorflow.examples.tutorials.mnist import input_data
    mnist = input_data.read_data_sets("MNIST_data")

    vae = VAE()
    vae.train(mnist, max_iter=100000, verbose=False)

if __name__ == "__main__":
    test_mnist()
