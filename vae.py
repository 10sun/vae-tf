from datetime import datetime
import os
import sys

import numpy as np
import tensorflow as tf

from layers import Dense
import plot
from utils import composeAll, print_


class VAE():
    """Variational Autoencoder

    see: Kingma & Welling - Auto-Encoding Variational Bayes
    (http://arxiv.org/pdf/1312.6114v10.pdf)
    """

    DEFAULTS = {
        "batch_size": 128,
        "learning_rate": 1E-3,
        "dropout": 1.,
        "lambda_l2_reg": 0.,
        "nonlinearity": tf.nn.tanh,
        "squashing": tf.nn.sigmoid,
    }
    RESTORE_KEY = "to_restore"

    def __init__(self, architecture, d_hyperparams={}, meta_graph=None,
                 save_graph_def=True, log_dir="./log", name=""):

        self.architecture = architecture
        self.__dict__.update(VAE.DEFAULTS, **d_hyperparams)
        self.sesh = tf.Session()

        if not meta_graph:
            # YYMMDD_HHMM
            self.datetime = "".join(c for c in str(datetime.today()) if c.isdigit()
                                    or c.isspace())[2:13].replace(" ", "_")
            if name:
                self.datetime += "_{}".format(name)

            # build graph
            handles = self._buildGraph()
            for handle in handles:
                tf.add_to_collection(VAE.RESTORE_KEY, handle)
            self.sesh.run(tf.initialize_all_variables())

        else:
            self.datetime = "{}_reloaded".format(os.path.basename(meta_graph)[:11])
            # rebuild graph
            meta_graph = os.path.abspath(meta_graph)
            tf.train.import_meta_graph(meta_graph + ".meta").restore(
                self.sesh, meta_graph)
            handles = self.sesh.graph.get_collection(VAE.RESTORE_KEY)

        # unpack handles for tensor ops to feed or fetch
        (self.x_in, self.dropout_, self.z_mean, self.z_log_sigma, self.z,
         self.x_reconstructed, self.z_, self.x_reconstructed_,
         self.cost, self.global_step, self.train_op) = handles

        if save_graph_def:
            self.logger = tf.train.SummaryWriter(log_dir, self.sesh.graph)

    @property
    def step(self):
        """Train step"""
        return self.global_step.eval(session=self.sesh)

    def _buildGraph(self):
        x_in = tf.placeholder(tf.float32, shape=[None, # enables variable batch size
                                                 self.architecture[0]], name="x")

        dropout = tf.placeholder_with_default(1., shape=[], name="dropout")

        # encoding / "recognition": q(z|x)
        # approximation of true posterior p(z|x) -- intractable to calculate
        encoding = [Dense("encoding", hidden_size, dropout, tf.nn.elu)
                    # hidden layers reversed for fn composition s.t. list reads outer -> inner
                    for hidden_size in reversed(self.architecture[1:-1])]
        h_encoded = composeAll(encoding)(x_in)

        # latent distribution Z from which X is generated, parameterized based on hidden encoding
        z_mean = Dense("z_mean", self.architecture[-1], dropout)(h_encoded)
        z_log_sigma = Dense("z_log_sigma", self.architecture[-1], dropout)(h_encoded)

        # let z ~ N(z_mean, np.exp(z_log_sigma)**2)
        # probabilistic decoder - given z, can observe distribution over corresponding x!
        # kingma & welling: only 1 draw per datapoint necessary as long as minibatch is large enough (>100)
        z = self.sampleGaussian(z_mean, z_log_sigma)

        # decoding / "generative": p(x|z)
        # assumes symmetric hidden architecture
        decoding = [Dense("decoding", hidden_size, dropout, tf.nn.elu)
                    for hidden_size in self.architecture[1:-1]]
        # prepend final reconstruction as outermost fn --> restore original dims, squash outputs [0, 1]
        decoding.insert(0, Dense("x_decoding", self.architecture[0], dropout, tf.nn.sigmoid))
        x_reconstructed = tf.identity(composeAll(decoding)(z), name="x_reconstructed")

        # optimization
        # goal: find variational & generative parameters that best reconstruct x
        # i.e. maximize log likelihood over observed datapoints
        # do this by maximizing (variational) lower bound on each marginal log likelihood
        # goal: increase (variational) lower bound on marginal log likelihood
        # loss
        # reconstruction loss, modeled as Bernoulli (i.e. with binary cross-entropy) / log likelihood
        rec_loss = VAE.crossEntropy(x_reconstructed, x_in)
        # rec_loss = VAE.l1_loss(x_reconstructed, x_in)
        # rec_loss = 0.5 * VAE.l2_loss(x_reconstructed, x_in) # "half of the euclidean error" = MSE
        rec_loss = print_(rec_loss, "rec")
        # Kullback-Leibler divergence: mismatch b/w approximate vs. imposed/true posterior
        # update variational distribution parameters / model's "wordview" to decrease "surprise"
        # as per http://www.logarithmic.net/pfh/blog/01133823191 / http://ilab.usc.edu/surprise
        kl_loss = VAE.kullbackLeibler(z_mean, z_log_sigma)
        kl_loss = print_(kl_loss, "kl")

        with tf.name_scope("l2_regularization"):
            regularizers = [tf.nn.l2_loss(var) for var in self.sesh.graph.get_collection(
                "trainable_variables") if "weights" in var.name]
            l2_reg = self.lambda_l2_reg * tf.add_n(regularizers)
            l2_reg = print_(l2_reg, "l2")

        # take mean over batch
        # weighting reconstruction loss by some alpha (0, 1) increases relative weight of prior
        cost = tf.reduce_mean(0.5 * rec_loss + kl_loss, name="cost") + l2_reg # TODO: weighting ?
        # cost = tf.add(rec_loss, kl_loss, name="cost")
        cost = print_(cost, "cost")

        global_step = tf.Variable(0, trainable=False)
        with tf.name_scope("Adam_optimizer"):
            optimizer = tf.train.AdamOptimizer(self.learning_rate)#, epsilon=1.)
            tvars = tf.trainable_variables()
            grads_and_vars = optimizer.compute_gradients(cost, tvars)
            # clipped = [(tf.clip_by_value(grad, -1, 1), tvar) # gradient clipping
            clipped = [(tf.clip_by_value(grad, -5, 5), tvar) # gradient clipping
                    for grad, tvar in grads_and_vars]
            # self.global_norm = print_(tf.global_norm(tvars), "global norm")
            # # grads, _ = tf.clip_by_global_norm(tf.gradients(cost, tvars), 1)
            train_op = optimizer.apply_gradients(clipped, global_step=global_step,
                                                 name="minimize_cost")
            # #train_op = optimizer.apply_gradients(list(zip(grads, tvars)))
            # train_op = (tf.train.AdamOptimizer(self.learning_rate)
            #                     .minimize(cost))
        self.numerics = tf.add_check_numerics_ops()
        # ops to directly explore latent space
        # defaults to prior z ~ N(0, I)
        z_ = tf.placeholder_with_default(tf.random_normal([1, self.architecture[-1]]),
                                         shape=[None, self.architecture[-1]],
                                         name="latent_in")
        x_reconstructed_ = composeAll(decoding)(z_)

        return (x_in, dropout, z_mean, z_log_sigma, z, x_reconstructed, z_,
                x_reconstructed_, cost, global_step, train_op)

    def sampleGaussian(self, mu, log_sigma):
        """Draw sample from Gaussian with given shape, subject to random noise epsilon"""
        # sample (estimated) prior
        with tf.name_scope("sample_gaussian"):
            # sampling / reparameterization trick
            epsilon = tf.random_normal(tf.shape(log_sigma), name="epsilon")
            # multivariate gaussian ~ N(mu, sigma**2*I)
            # z ~ p(z|x)
            return mu + epsilon * tf.exp(log_sigma)

    @staticmethod
    def crossEntropy(obs, actual, offset=1e-7):
        # (tf.Tensor, tf.Tensor, float) -> tf.Tensor
        # binary cross-entropy - assumes p(x|z) is a multivariate Bernoulli
        with tf.name_scope("cross_entropy"):
            # bound by clipping to avoid nan
            obs_ = tf.clip_by_value(obs, offset, 1 - offset)
            return -tf.reduce_sum(actual * tf.log(obs_) +
                                  (1 - actual) * tf.log(1 - obs_), 1)

    @staticmethod
    def l1_loss(obs, actual):
        # (tf.Tensor, tf.Tensor, float) -> tf.Tensor
        with tf.name_scope("l1_loss"):
            return tf.reduce_sum(tf.abs(obs - actual) , 1)

    @staticmethod
    def l2_loss(obs, actual):
        # (tf.Tensor, tf.Tensor, float) -> tf.Tensor
        # if averaged = MSE
        with tf.name_scope("l2_loss"):
            return tf.reduce_sum(tf.square(obs - actual), 1)

    @staticmethod
    def kullbackLeibler(mu, log_sigma):
        # (tf.Tensor, tf.Tensor) -> tf.Tensor
        # equiv to 0.5 * (1 + log(sigma**2) - mu**2 - sigma**2), summed over dims of latent space
        with tf.name_scope("KL_divergence"):
            return -0.5 * tf.reduce_sum(1 + 2 * log_sigma - mu**2 - tf.exp(2 * log_sigma), 1)

    def encode(self, x):
        """Encoder from inputs to latent distribution parameters"""
        # np.array -> [float, float]
        feed_dict = {self.x_in: x}
        return self.sesh.run([self.z_mean, self.z_log_sigma], feed_dict=feed_dict)

    def decode(self, zs=None):
        """Generative decoder from latent space to reconstructions of input space"""
        # np.array or tf.Variable -> np.array
        feed_dict = dict()

        if zs != None:
            is_tensor = lambda x: hasattr(x, "eval")
            zs = (self.sesh.run(zs) if is_tensor(zs) else zs)
            feed_dict.update({self.z_: zs})

        return self.sesh.run(self.x_reconstructed_, feed_dict=feed_dict)

    def vae(self, x):
        """End-to-end autoencoder"""
        # np.array -> np.array
        return self.decode(self.sampleGaussian(*self.encode(x)))

    def train(self, X, max_iter=np.inf, max_epochs=np.inf, cross_validate=True,
              verbose=True, save=False, outdir="./out", plots_outdir="./png"):
        if save:
            saver = tf.train.Saver(tf.all_variables())

        try:
            err_train = 0
            #err_cv = 0
            now = datetime.now().isoformat()[11:]
            print("------- Training begin: {} -------\n".format(now))

            while True:
                x, _ = X.train.next_batch(self.batch_size)
                feed_dict = {self.x_in: x, self.dropout_: self.dropout}
                fetches = [self.x_reconstructed, self.cost, self.global_step, self.train_op]
                x_reconstructed, cost, i, _ = self.sesh.run(fetches, feed_dict)

                err_train += cost

                if i%1000 == 0 and verbose:
                    print("round {} --> avg cost: ".format(i), err_train / i)

                if i%2000 == 0 and verbose and i >= 10000:
                    plot.plotSubset(self, x, x_reconstructed, n=10, name="train",
                                    outdir=plots_outdir)

                    if cross_validate:
                        x, _ = X.validation.next_batch(self.batch_size)
                        feed_dict = {self.x_in: x}
                        fetches = [self.x_reconstructed, self.cost]
                        x_reconstructed, cost = self.sesh.run(fetches, feed_dict)

                        #err_cv += cost
                        print("round {} --> CV cost: ".format(i), cost)
                        plot.plotSubset(self, x, x_reconstructed, n=10, name="cv",
                                        outdir=plots_outdir)

                if i >= max_iter or X.train.epochs_completed >= max_epochs:
                    print("final avg cost (@ step {} = epoch {}): {}".format(
                        i, X.train.epochs_completed, err_train / i))
                    now = datetime.now().isoformat()[11:]
                    print("------- Training end: {} -------\n".format(now))

                    if save:
                        outfile = os.path.join(os.path.abspath(outdir), "{}_vae_{}".format(
                            self.datetime, "_".join(map(str, self.architecture))))
                        saver.save(self.sesh, outfile, global_step=self.step)
                    try:
                        self.logger.flush()
                        self.logger.close()
                    except(AttributeError): # not logging
                        continue
                    break

        except(KeyboardInterrupt):
            print("final avg cost (@ step {} = epoch {}): {}".format(
                i, X.train.epochs_completed, err_train / i))
            now = datetime.now().isoformat()[11:]
            print("------- Training end: {} -------\n".format(now))
            sys.exit(0)
