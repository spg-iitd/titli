import torch
import pickle
import numpy as np

from scipy.stats import norm
from scipy.cluster.hierarchy import linkage, to_tree

from walle.ids.torch_kitnet import _TorchKitNET # type: ignore

class KitNET:
    """    
        n                   : the number of features in your input dataset (i.e., x \in R^n)
        max_autoencoder_size: the maximum size of any autoencoder in the ensemble layer
        FM_grace_period     : the number of instances which will be taken to learn the feature mapping.
                              If 'None', then FM_grace_period=AM_grace_period
        AD_grace_period     : the number of instances the network will learn from before producing
                              anomaly scores
        learning_rate       : stochastic gradient descent learning rate for all autoencoders.
        hidden_ratio        : the default ratio of hidden to visible neurons.
        feature_map         : One may optionally provide a feature map instead of learning one. The map must be
                              a list, where the i-th entry contains a list of the feature indices to be assingned
                              to the i-th autoencoder in the ensemble. For example, [[2,5,3],[4,0,1],[6,7]]
        normalize           : boolean, whether to 0-1 normalize the input data (default: True)
        input_precision     : integer, number of significant figures in the input data
        quantize            : boolean, whether to quantize the input data (default: None)
        model_path          : path to save the model
    """
    def __init__(self, n,
                 max_autoencoder_size=10,
                 FM_grace_period=None,
                 AD_grace_period=10000,
                 learning_rate=0.1,
                 hidden_ratio=0.75,
                 feature_map=None,
                 normalize=True,
                 input_precision=None,
                 quantize=None,
                 model_path="kitsune.pkl"):

        self.AD_grace_period = AD_grace_period
        if FM_grace_period is None:
            self.FM_grace_period = AD_grace_period
        else:
            self.FM_grace_period = FM_grace_period
        self.input_precision = input_precision
        if max_autoencoder_size <= 0:
            self.m = 1
        else:
            self.m = max_autoencoder_size
        self.lr = learning_rate
        self.hr = hidden_ratio
        self.n = n
        self.normalize = normalize
        # Variables
        self.n_trained = 0  # the number of training instances so far
        self.n_executed = 0  # the number of executed instances so far
        self.v = feature_map
        self.ensembleLayer = []
        self.outputLayer = None
        self.quantize = quantize
        self.model_path = model_path
        self.norm_params_path = model_path.replace(".pkl", "_norm_params.pkl")
        if self.v is None:
            pass
            print("Feature-Mapper: train-mode, Anomaly-Detector: off-mode")
        else:
            self.__createAD__()
            print("Feature-Mapper: execute-mode, Anomaly-Detector: train-mode")
        # incremental feature clustering for the feature mapping process
        self.FM = corClust(self.n)

    def process(self, x):
        """
        If FM_grace_period+AM_grace_period has passed, then this function executes KitNET on x.
        Otherwise, this function learns from x.
        x: a np array of length n
        Note: KitNET automatically performs 0-1 normalization on all attributes.
        """
        # if all -1 it means the packet was ignored
        if x.all() == -1:
            return 0.

        if self.n_trained > self.FM_grace_period + self.AD_grace_period:  # If both the FM and AD are in execute-mode
            return self.execute(x)
        else:
            self.train(x)
            return 0.0
 
    def decision_function(self, x):
        """
        alias so it is compatible with sklearn models, input and output are all 2d arrays
        """
        anom_score = self.process(x[0])
        return [-anom_score]

    def predict(self, x):
        """
        alias for execute for it is compatible with tf models, processes in batches
        """
        return np.array([self.process(x[i]) for i in range(np.array(x).shape[0])])

    def train(self, x):
        """
    Trains the model on instance 'x'. Updates the correlation matrix during the grace period, 
    trains the ensemble and output layers otherwise. Also saves the min and max norms to a file.

    Parameters:
    x (numpy array): The instance for training.

    Returns:
    None
    """
        # If the FM is in train-mode, and the user has not supplied a feature mapping
        if self.n_trained <= self.FM_grace_period and self.v is None:
            # update the incremetnal correlation matrix
            self.FM.update(x)
            if self.n_trained == self.FM_grace_period:  # If the feature mapping should be instantiated
                self.v = self.FM.cluster(self.m)
                self.__createAD__()
                print("The Feature-Mapper found a mapping: "+str(self.n)+ \
                      " features to "+str(len(self.v))+" autoencoders.")
                print("Feature-Mapper: execute-mode, Anomaly-Detector: train-mode")
        else:  # train
            # Ensemble Layer
            S_l1 = np.zeros(len(self.ensembleLayer))
            for a in range(len(self.ensembleLayer)):
                # make sub instance for autoencoder 'a'
                xi = x[self.v[a]]
                S_l1[a] = self.ensembleLayer[a].train(xi)
            # OutputLayer
            rmse = self.outputLayer.train(S_l1)

            """save ensemble and output layer norms"""
            norm_params = {}
            for a in range(len(self.ensembleLayer)):
                norm_params[f"norm_min_{self.v[a][0]}"] = self.ensembleLayer[a].norm_min
                norm_params[f"norm_max_{self.v[a][0]}"] = self.ensembleLayer[a].norm_max
            
            norm_params["norm_min_output"] = self.outputLayer.norm_min
            norm_params["norm_max_output"] = self.outputLayer.norm_max

            with open(self.norm_params_path, 'wb') as f:
                pickle.dump(norm_params, f)

            if self.n_trained == self.AD_grace_period + self.FM_grace_period:
                pass
                print("Feature-Mapper: execute-mode, Anomaly-Detector: execute-mode")
        self.n_trained += 1

    def execute(self, x):
        if self.v is None:
            raise RuntimeError(
                'KitNET Cannot execute x, because a feature mapping has not yet been learned \
                 or provided. Try running process(x) instead.')
        else:
            self.n_executed += 1
            # Ensemble Layer
            S_l1 = np.zeros(len(self.ensembleLayer))
            for a in range(len(self.ensembleLayer)):
                # make sub inst
                xi = x[self.v[a]]
                S_l1[a] = self.ensembleLayer[a].execute(xi)
            # OutputLayer
            return self.outputLayer.execute(S_l1)

    def __createAD__(self):
        # construct ensemble layer
        for map in self.v:
            params = dA_params(n_visible=len(map), n_hidden=0, lr=self.lr, corruption_level=0, gracePeriod=0,
                                  hiddenRatio=self.hr, normalize=self.normalize,
                                  input_precision=self.input_precision, quantize=self.quantize)
            self.ensembleLayer.append(dA(params))

        # construct output layer
        params = dA_params(len(self.v), n_hidden=0, lr=self.lr, corruption_level=0, gracePeriod=0, hiddenRatio=self.hr,
                              normalize=self.normalize, quantize=self.quantize, input_precision=self.input_precision)
        self.outputLayer = dA(params)

    def get_params(self):
        return_dict = {"ensemble": []}
        for i in range(len(self.ensembleLayer)):
            return_dict["ensemble"].append(self.ensembleLayer[i].get_params())
        return_dict["output"] = self.outputLayer.get_params()
        return return_dict

    def set_params(self, new_param):
        for i in range(len(new_param["ensemble"])):
            self.ensembleLayer[i].set_params(new_param["ensemble"][i])
        self.outputLayer.set_params(new_param["output"])

    def get_torch_model(self):
        weights = self.get_params()
        model = _TorchKitNET(weights["ensemble"], weights["output"], self.v, self.n)
        path = self.model_path.replace(".pkl", ".pth")
        torch.save(model.state_dict(), path)

def squeeze_features(fv, precision):
    """rounds features to siginificant figures

    Args:
        fv (array): feature vector.
        precision (int): number of precisions to use.

    Returns:
        array: rounded array of floats.

    """

    return np.around(fv, decimals=precision)

def quantize(x, k):
    n = 2**k - 1
    return np.round(np.multiply(n, x))/n

def quantize_weights(w, k):
    x = np.tanh(w)
    q = x / np.max(np.abs(x)) * 0.5 + 0.5
    return 2 * quantize(q, k) - 1


class dA_params:
    def __init__(self, n_visible=5,
                 n_hidden=3,
                 lr=0.001,
                 corruption_level=0.0,
                 gracePeriod=10000,
                 hiddenRatio=None,
                 normalize=True,
                 input_precision=None,
                 quantize=None):

        self.n_visible = n_visible  # num of units in visible (input) layer
        self.n_hidden = n_hidden  # num of units in hidden layer
        self.lr = lr
        self.corruption_level = corruption_level
        self.gracePeriod = gracePeriod
        self.hiddenRatio = hiddenRatio
        self.normalize = normalize
        self.quantize=quantize
        self.input_precision=input_precision
        if quantize:
            self.q_wbit,self.q_abit=quantize


class dA:
    def __init__(self, params):
        self.params = params

        if self.params.hiddenRatio is not None:
            self.params.n_hidden = int(np.ceil(
                self.params.n_visible * self.params.hiddenRatio))

        # for 0-1 normlaization
        self.norm_max = np.ones((self.params.n_visible,)) * -np.Inf
        self.norm_min = np.ones((self.params.n_visible,)) * np.Inf
        self.n = 0

        self.rng = np.random.RandomState(1234)

        a = 1. / self.params.n_visible
        self.W = np.array(self.rng.uniform(  # initialize W uniformly
            low=-a,
            high=a,
            size=(self.params.n_visible, self.params.n_hidden)))

        #quantize weights
        if self.params.quantize:
            self.W=quantize_weights(self.W, self.params.q_wbit)

        self.hbias = np.zeros(self.params.n_hidden)  # initialize h bias 0
        self.vbias = np.zeros(self.params.n_visible)  # initialize v bias 0
        # self.W_prime = self.W.T

    def get_corrupted_input(self, input, corruption_level):
        assert corruption_level < 1

        return self.rng.binomial(size=input.shape,
                                 n=1,
                                 p=1 - corruption_level) * input

    # Encode
    def get_hidden_values(self, input):
        return sigmoid(np.dot(input, self.W) + self.hbias)

    # Decode
    def get_reconstructed_input(self, hidden):
        return sigmoid(np.dot(hidden, self.W.T) + self.vbias)

    def train(self, x):
        self.n = self.n + 1

        if self.params.normalize:
            # update norms
            self.norm_max[x > self.norm_max] = x[x > self.norm_max]
            self.norm_min[x < self.norm_min] = x[x < self.norm_min]

            # 0-1 normalize
            x = (x - self.norm_min) / (self.norm_max -
                                       self.norm_min + 0.0000000000000001)

        if self.params.input_precision:
            x=squeeze_features(x,self.params.input_precision)

        if self.params.corruption_level > 0.0:
            tilde_x = self.get_corrupted_input(x, self.params.corruption_level)
        else:
            tilde_x = x

        y = self.get_hidden_values(tilde_x)
        if self.params.quantize:
            y=quantize(y, self.params.q_abit)

        z = self.get_reconstructed_input(y)

        L_h2 = x - z
        L_h1 = np.dot(L_h2, self.W) * y * (1 - y)

        L_vbias = L_h2
        L_hbias = L_h1
        L_W = np.outer(tilde_x.T, L_h1) + np.outer(L_h2.T, y)

        self.W += self.params.lr * L_W
        self.hbias += self.params.lr * L_hbias
        self.vbias += self.params.lr * L_vbias

        if self.params.quantize:
            self.W=quantize_weights(self.W, self.params.q_wbit)
            self.hbias=quantize_weights(self.hbias, self.params.q_wbit)
            self.vbias=quantize_weights(self.vbias, self.params.q_wbit)

        return np.sqrt(np.mean(L_h2**2))

    def reconstruct(self, x):
        y = self.get_hidden_values(x)

        try:
            if self.params.quantize:
                y=quantize(y, self.params.q_abit)
        except AttributeError as e:
            pass
            
        z = self.get_reconstructed_input(y)
        return z

    def get_params(self):
        params={
        "W":self.W,
        "hbias":self.hbias,
        "vbias":self.vbias
        }
        return params

    def set_params(self, new_param):
        self.W=new_param["W"]
        self.hbias=new_param["hbias"]
        self.vbias=new_param["vbias"]

    def execute(self, x):
        if self.n < self.params.gracePeriod:
            return 0.0
        else:
            # 0-1 normalize
            try:
                if self.params.normalize:
                    x = (x - self.norm_min) / (self.norm_max -
                                               self.norm_min + 0.0000000000000001)

                if self.params.input_precision:
                    x=squeeze_features(x,self.params.input_precision)

            except AttributeError as e:
                pass

            z = self.reconstruct(x)
            rmse = np.sqrt(((x - z) ** 2).mean())  # MSE
            return rmse

    def inGrace(self):
        return self.n < self.params.gracePeriod

def pdf(x,mu,sigma): #normal distribution pdf
    x = (x-mu)/sigma
    return np.exp(-x**2/2)/(np.sqrt(2*np.pi)*sigma)

def invLogCDF(x,mu,sigma): #normal distribution cdf
    x = (x - mu) / sigma
    return norm.logcdf(-x) #note: we mutiple by -1 after normalization to better get the 1-cdf

def sigmoid(x):
    return 1. / (1 + np.exp(-x))

def dsigmoid(x):
    return x * (1. - x)

def tanh(x):
    return np.tanh(x)

def dtanh(x):
    return 1. - x * x

def softmax(x):
    e = np.exp(x - np.max(x))  # prevent overflow
    if e.ndim == 1:
        return e / np.sum(e, axis=0)
    else:  
        return e / np.array([np.sum(e, axis=1)]).T  # ndim = 2

def ReLU(x):
    return x * (x > 0)

def dReLU(x):
    return 1. * (x > 0)

class rollmean:
    def __init__(self,k):
        self.winsize = k
        self.window = np.zeros(self.winsize)
        self.pointer = 0

    def apply(self,newval):
        self.window[self.pointer]=newval
        self.pointer = (self.pointer+1) % self.winsize
        return np.mean(self.window)


class corClust:
    """
        A helper class for KitNET which performs a correlation-based incremental 
        clustering of the dimensions in X

        # n: the number of dimensions in the dataset
        # For more information and citation, please see our NDSS'18 paper:
        Kitsune: An Ensemble of Autoencoders for Online Network Intrusion Detection
    """
    def __init__(self,n):
        #parameter:
        self.n = n
        #varaibles
        self.c = np.zeros(n) #linear num of features
        self.c_r = np.zeros(n) #linear sum of feature residules
        self.c_rs = np.zeros(n) #linear sum of feature residules
        self.C = np.zeros((n,n)) #partial correlation matrix
        self.N = 0 #number of updates performed

    # x: a numpy vector of length n
    def update(self,x):
        self.N += 1
        self.c += x
        c_rt = x - self.c/self.N
        self.c_r += c_rt
        self.c_rs += c_rt**2
        self.C += np.outer(c_rt,c_rt)

    # creates the current correlation distance matrix between the features
    def corrDist(self):
        c_rs_sqrt = np.sqrt(self.c_rs)
        C_rs_sqrt = np.outer(c_rs_sqrt,c_rs_sqrt)
        C_rs_sqrt[C_rs_sqrt==0] = 1e-100 #this protects against dive by zero erros (occurs when a feature is a constant)
        D = 1-self.C/C_rs_sqrt #the correlation distance matrix
        D[D<0] = 0 #small negatives may appear due to the incremental fashion in which we update the mean. Therefore, we 'fix' them
        return D

    # clusters the features together, having no more than maxClust features per cluster
    def cluster(self,maxClust):
        D = self.corrDist()
        Z = linkage(D[np.triu_indices(self.n, 1)])  # create a linkage matrix based on the distance matrix
        if maxClust < 1:
            maxClust = 1
        if maxClust > self.n:
            maxClust = self.n
        map = self.__breakClust__(to_tree(Z),maxClust)
        return map

    # a recursive helper function which breaks down the dendrogram branches until all clusters have no more than maxClust elements
    def __breakClust__(self,dendro,maxClust):
        if dendro.count <= maxClust: #base case: we found a minimal cluster, so mark it
            return [dendro.pre_order()] #return the origional ids of the features in this cluster
        return self.__breakClust__(dendro.get_left(),maxClust) + self.__breakClust__(dendro.get_right(),maxClust)
