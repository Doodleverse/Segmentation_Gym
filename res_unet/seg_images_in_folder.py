# Written by Dr Daniel Buscombe, Marda Science LLC
# for the USGS Coastal Change Hazards Program
#
# MIT License
#
# Copyright (c) 2020, Marda Science LLC
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import os, time

USE_GPU = True #False #True
# DO_CRF_REFINE = True

if USE_GPU == True:
   ##use the first available GPU
   os.environ['CUDA_VISIBLE_DEVICES'] = '0' #'1'
else:
   ## to use the CPU (not recommended):
   os.environ['CUDA_VISIBLE_DEVICES'] = '-1'


#suppress tensorflow warnings
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'

#utils
#keras functions for early stopping and model weights saving
from tensorflow.keras.callbacks import EarlyStopping, ModelCheckpoint
import numpy as np
import tensorflow as tf #numerical operations on gpu
from joblib import Parallel, delayed
from numpy.lib.stride_tricks import as_strided as ast
from skimage.morphology import remove_small_holes, remove_small_objects
from skimage.restoration import inpaint
from scipy.ndimage import maximum_filter
from skimage.transform import resize
from tqdm import tqdm
from skimage.filters import threshold_otsu

SEED=42
np.random.seed(SEED)
AUTO = tf.data.experimental.AUTOTUNE # used in tf.data.Dataset API

tf.random.set_seed(SEED)

print("Version: ", tf.__version__)
print("Eager mode: ", tf.executing_eagerly())
print('GPU name: ', tf.config.experimental.list_physical_devices('GPU'))
print("Num GPUs Available: ", len(tf.config.experimental.list_physical_devices('GPU')))

import tensorflow.keras.backend as K
import pydensecrf.densecrf as dcrf
from pydensecrf.utils import create_pairwise_bilateral, unary_from_labels
from skimage.filters.rank import median
from skimage.morphology import disk

from tkinter import filedialog
from tkinter import *
import json
from skimage.io import imsave
from skimage.transform import resize


#-----------------------------------
def crf_refine(label, img, nclasses,theta_col=100, mu=120, theta_spat=3, mu_spat=3):
    """
    "crf_refine(label, img)"
    This function refines a label image based on an input label image and the associated image
    Uses a conditional random field algorithm using spatial and image features
    INPUTS:
        * label [ndarray]: label image 2D matrix of integers
        * image [ndarray]: image 3D matrix of integers
    OPTIONAL INPUTS: None
    GLOBAL INPUTS: None
    OUTPUTS: label [ndarray]: label image 2D matrix of integers
    """

    H = label.shape[0]
    W = label.shape[1]
    U = unary_from_labels(1+label,nclasses,gt_prob=0.51)
    d = dcrf.DenseCRF2D(H, W, nclasses)
    d.setUnaryEnergy(U)

    # to add the color-independent term, where features are the locations only:
    d.addPairwiseGaussian(sxy=(theta_spat, theta_spat),
                 compat=mu_spat,
                 kernel=dcrf.DIAG_KERNEL,
                 normalization=dcrf.NORMALIZE_SYMMETRIC)
    feats = create_pairwise_bilateral(
                          sdims=(theta_col, theta_col),
                          schan=(2,2,2),
                          img=img,
                          chdim=2)

    d.addPairwiseEnergy(feats, compat=mu,kernel=dcrf.DIAG_KERNEL,normalization=dcrf.NORMALIZE_SYMMETRIC)
    Q = d.inference(10)
    #kl1 = d.klDivergence(Q)
    return np.argmax(Q, axis=0).reshape((H, W)).astype(np.uint8)#, kl1

#-----------------------------------
def mean_iou(y_true, y_pred):
    """
    mean_iou(y_true, y_pred)
    This function computes the mean IoU between `y_true` and `y_pred`: this version is tensorflow (not numpy) and is used by tensorflow training and evaluation functions

    INPUTS:
        * y_true: true masks, one-hot encoded.
            * Inputs are B*W*H*N tensors, with
                B = batch size,
                W = width,
                H = height,
                N = number of classes
        * y_pred: predicted masks, either softmax outputs, or one-hot encoded.
            * Inputs are B*W*H*N tensors, with
                B = batch size,
                W = width,
                H = height,
                N = number of classes
    OPTIONAL INPUTS: None
    GLOBAL INPUTS: None
    OUTPUTS:
        * IoU score [tensor]
    """
    yt0 = y_true[:,:,:,0]
    yp0 = tf.keras.backend.cast(y_pred[:,:,:,0] > 0.5, 'float32')
    inter = tf.math.count_nonzero(tf.logical_and(tf.equal(yt0, 1), tf.equal(yp0, 1)))
    union = tf.math.count_nonzero(tf.add(yt0, yp0))
    iou = tf.where(tf.equal(union, 0), 1., tf.cast(inter/union, 'float32'))
    return iou

#-----------------------------------
def dice_coef(y_true, y_pred):
    """
    dice_coef(y_true, y_pred)

    This function computes the mean Dice coefficient between `y_true` and `y_pred`: this version is tensorflow (not numpy) and is used by tensorflow training and evaluation functions

    INPUTS:
        * y_true: true masks, one-hot encoded.
            * Inputs are B*W*H*N tensors, with
                B = batch size,
                W = width,
                H = height,
                N = number of classes
        * y_pred: predicted masks, either softmax outputs, or one-hot encoded.
            * Inputs are B*W*H*N tensors, with
                B = batch size,
                W = width,
                H = height,
                N = number of classes
    OPTIONAL INPUTS: None
    GLOBAL INPUTS: None
    OUTPUTS:
        * Dice score [tensor]
    """
    smooth = 1.
    y_true_f = tf.reshape(tf.dtypes.cast(y_true, tf.float32), [-1])
    y_pred_f = tf.reshape(tf.dtypes.cast(y_pred, tf.float32), [-1])
    intersection = tf.reduce_sum(y_true_f * y_pred_f)
    return (2. * intersection + smooth) / (tf.reduce_sum(y_true_f) + tf.reduce_sum(y_pred_f) + smooth)


##========================================================
def rescale(dat,
    mn,
    mx):
    '''
    rescales an input dat between mn and mx
    '''
    m = min(dat.flatten())
    M = max(dat.flatten())
    return (mx-mn)*(dat-m)/(M-m)+mn


##====================================
def standardize(img):
    #standardization using adjusted standard deviation
    N = np.shape(img)[0] * np.shape(img)[1]
    s = np.maximum(np.std(img), 1.0/np.sqrt(N))
    m = np.mean(img)
    img = (img - m) / s
    img = rescale(img, 0, 1)
    del m, s, N

    if np.ndim(img)!=3:
        img = np.dstack((img,img,img))

    return img

###############################################################
### MODEL FUNCTIONS
###############################################################
#-----------------------------------
def batchnorm_act(x):
    """
    batchnorm_act(x)
    This function applies batch normalization to a keras model layer, `x`, then a relu activation function
    INPUTS:
        * `z` : keras model layer (should be the output of a convolution or an input layer)
    OPTIONAL INPUTS: None
    GLOBAL INPUTS: None
    OUTPUTS:
        * batch normalized and relu-activated `x`
    """
    x = tf.keras.layers.BatchNormalization()(x)
    return tf.keras.layers.Activation("relu")(x)

#-----------------------------------
def conv_block(x, filters, kernel_size = (7,7), padding="same", strides=1):
    """
    conv_block(x, filters, kernel_size = (7,7), padding="same", strides=1)
    This function applies batch normalization to an input layer, then convolves with a 2D convol layer
    The two actions combined is called a convolutional block

    INPUTS:
        * `filters`: number of filters in the convolutional block
        * `x`:input keras layer to be convolved by the block
    OPTIONAL INPUTS:
        * `kernel_size`=(3, 3): tuple of kernel size (x, y) - this is the size in pixels of the kernel to be convolved with the image
        * `padding`="same":  see tf.keras.layers.Conv2D
        * `strides`=1: see tf.keras.layers.Conv2D
    GLOBAL INPUTS: None
    OUTPUTS:
        * keras layer, output of the batch normalized convolution
    """
    conv = batchnorm_act(x)
    return tf.keras.layers.Conv2D(filters, kernel_size, padding=padding, strides=strides)(conv)

#-----------------------------------
def bottleneck_block(x, filters, kernel_size = (7,7), padding="same", strides=1):
    """
    bottleneck_block(x, filters, kernel_size = (7,7), padding="same", strides=1)

    This function creates a bottleneck block layer, which is the addition of a convolution block and a batch normalized/activated block
    INPUTS:
        * `filters`: number of filters in the convolutional block
        * `x`: input keras layer
    OPTIONAL INPUTS:
        * `kernel_size`=(3, 3): tuple of kernel size (x, y) - this is the size in pixels of the kernel to be convolved with the image
        * `padding`="same":  see tf.keras.layers.Conv2D
        * `strides`=1: see tf.keras.layers.Conv2D
    GLOBAL INPUTS: None
    OUTPUTS:
        * keras layer, output of the addition between convolutional and bottleneck layers
    """
    conv = tf.keras.layers.Conv2D(filters, kernel_size, padding=padding, strides=strides)(x)
    conv = conv_block(conv, filters, kernel_size=kernel_size, padding=padding, strides=strides)

    bottleneck = tf.keras.layers.Conv2D(filters, kernel_size=(1, 1), padding=padding, strides=strides)(x)
    bottleneck = batchnorm_act(bottleneck)

    return tf.keras.layers.Add()([conv, bottleneck])

#-----------------------------------
def res_block(x, filters, kernel_size = (7,7), padding="same", strides=1):
    """
    res_block(x, filters, kernel_size = (7,7), padding="same", strides=1)

    This function creates a residual block layer, which is the addition of a residual convolution block and a batch normalized/activated block
    INPUTS:
        * `filters`: number of filters in the convolutional block
        * `x`: input keras layer
    OPTIONAL INPUTS:
        * `kernel_size`=(3, 3): tuple of kernel size (x, y) - this is the size in pixels of the kernel to be convolved with the image
        * `padding`="same":  see tf.keras.layers.Conv2D
        * `strides`=1: see tf.keras.layers.Conv2D
    GLOBAL INPUTS: None
    OUTPUTS:
        * keras layer, output of the addition between residual convolutional and bottleneck layers
    """
    res = conv_block(x, filters, kernel_size=kernel_size, padding=padding, strides=strides)
    res = conv_block(res, filters, kernel_size=kernel_size, padding=padding, strides=1)

    bottleneck = tf.keras.layers.Conv2D(filters, kernel_size=(1, 1), padding=padding, strides=strides)(x)
    bottleneck = batchnorm_act(bottleneck)

    return tf.keras.layers.Add()([bottleneck, res])

#-----------------------------------
def upsamp_concat_block(x, xskip):
    """
    upsamp_concat_block(x, xskip)
    This function takes an input layer and creates a concatenation of an upsampled version and a residual or 'skip' connection
    INPUTS:
        * `xskip`: input keras layer (skip connection)
        * `x`: input keras layer
    OPTIONAL INPUTS: None
    GLOBAL INPUTS: None
    OUTPUTS:
        * keras layer, output of the addition between residual convolutional and bottleneck layers
    """
    u = tf.keras.layers.UpSampling2D((2, 2))(x)
    return tf.keras.layers.Concatenate()([u, xskip])

#-----------------------------------
def iou(obs, est, nclasses):
    IOU=0
    for n in range(1,nclasses+1):
        component1 = obs==n
        component2 = est==n
        overlap = component1*component2 # Logical AND
        union = component1 + component2 # Logical OR
        calc = overlap.sum()/float(union.sum())
        if not np.isnan(calc):
            IOU += calc
        if IOU>1:
            IOU=IOU/n
    return IOU

#-----------------------------------
def res_unet(sz, f, nclasses=1):
    """
    res_unet(sz, f, nclasses=1)
    This function creates a custom residual U-Net model for image segmentation
    INPUTS:
        * `sz`: [tuple] size of input image
        * `f`: [int] number of filters in the convolutional block
        * flag: [string] if 'binary', the model will expect 2D masks and uses sigmoid. If 'multiclass', the model will expect 3D masks and uses softmax
        * nclasses [int]: number of classes
    OPTIONAL INPUTS:
        * `kernel_size`=(3, 3): tuple of kernel size (x, y) - this is the size in pixels of the kernel to be convolved with the image
        * `padding`="same":  see tf.keras.layers.Conv2D
        * `strides`=1: see tf.keras.layers.Conv2D
    GLOBAL INPUTS: None
    OUTPUTS:
        * keras model
    """
    inputs = tf.keras.layers.Input(sz)

    ## downsample
    e1 = bottleneck_block(inputs, f); f = int(f*2)
    e2 = res_block(e1, f, strides=2); f = int(f*2)
    e3 = res_block(e2, f, strides=2); f = int(f*2)
    e4 = res_block(e3, f, strides=2); f = int(f*2)
    _ = res_block(e4, f, strides=2)

    ## bottleneck
    b0 = conv_block(_, f, strides=1)
    _ = conv_block(b0, f, strides=1)

    ## upsample
    _ = upsamp_concat_block(_, e4)
    _ = res_block(_, f); f = int(f/2)

    _ = upsamp_concat_block(_, e3)
    _ = res_block(_, f); f = int(f/2)

    _ = upsamp_concat_block(_, e2)
    _ = res_block(_, f); f = int(f/2)

    _ = upsamp_concat_block(_, e1)
    _ = res_block(_, f)

    ## classify
    if nclasses==1:
        outputs = tf.keras.layers.Conv2D(nclasses, (1, 1), padding="same", activation="sigmoid")(_)
    else:
        outputs = tf.keras.layers.Conv2D(nclasses, (1, 1), padding="same", activation="softmax")(_)

    #model creation
    model = tf.keras.models.Model(inputs=[inputs], outputs=[outputs])
    return model

#-----------------------------------
def seg_file2tensor_3band(f, resize):
    """
    "seg_file2tensor(f)"
    This function reads a jpeg image from file into a cropped and resized tensor,
    for use in prediction with a trained segmentation model
    INPUTS:
        * f [string] file name of jpeg
    OPTIONAL INPUTS: None
    OUTPUTS:
        * image [tensor array]: unstandardized image
    GLOBAL INPUTS: TARGET_SIZE
    """
    bits = tf.io.read_file(f)
    if 'jpg' in f:
        bigimage = tf.image.decode_jpeg(bits)
    elif 'png' in f:
        bigimage = tf.image.decode_png(bits)

    if USE_LOCATION:
        gx,gy = np.meshgrid(np.arange(bigimage.shape[1]), np.arange(bigimage.shape[0]))
        loc = np.sqrt(gx**2 + gy**2)
        loc /= loc.max()
        loc = (255*loc).astype('uint8')
        bigimage = np.dstack((bigimage, loc))

    w = tf.shape(bigimage)[0]
    h = tf.shape(bigimage)[1]

    if resize:

        tw = TARGET_SIZE[0]
        th = TARGET_SIZE[1]
        resize_crit = (w * th) / (h * tw)
        image = tf.cond(resize_crit < 1,
                      lambda: tf.image.resize(bigimage, [w*tw/w, h*tw/w]), # if true
                      lambda: tf.image.resize(bigimage, [w*th/h, h*th/h])  # if false
                     )

        nw = tf.shape(image)[0]
        nh = tf.shape(image)[1]
        image = tf.image.crop_to_bounding_box(image, (nw - tw) // 2, (nh - th) // 2, tw, th)
        # image = tf.cast(image, tf.uint8) #/ 255.0


    return image, w, h, bigimage


#-----------------------------------
def seg_file2tensor_4band(f, fir, resize):
    """
    "seg_file2tensor(f)"
    This function reads a jpeg image from file into a cropped and resized tensor,
    for use in prediction with a trained segmentation model
    INPUTS:
        * f [string] file name of jpeg
    OPTIONAL INPUTS: None
    OUTPUTS:
        * image [tensor array]: unstandardized image
    GLOBAL INPUTS: TARGET_SIZE
    """
    bits = tf.io.read_file(f)

    if 'jpg' in f:
        bigimage = tf.image.decode_jpeg(bits)
    elif 'png' in f:
        bigimage = tf.image.decode_png(bits)

    bits = tf.io.read_file(fir)
    if 'jpg' in fir:
        nir = tf.image.decode_jpeg(bits)
    elif 'png' in f:
        nir = tf.image.decode_png(bits)

    if USE_LOCATION:
        gx,gy = np.meshgrid(np.arange(bigimage.shape[1]), np.arange(bigimage.shape[0]))
        loc = np.sqrt(gx**2 + gy**2)
        loc /= loc.max()
        loc = (255*loc).astype('uint8')
        bigimage = np.dstack((bigimage, loc))

    if USE_LOCATION:
        bigimage = tf.concat([bigimage, nir],-1)[:,:,:N_DATA_BANDS+1]
    else:
        bigimage = tf.concat([bigimage, nir],-1)[:,:,:N_DATA_BANDS]

    w = tf.shape(bigimage)[0]
    h = tf.shape(bigimage)[1]

    if resize:

        tw = TARGET_SIZE[0]
        th = TARGET_SIZE[1]
        resize_crit = (w * th) / (h * tw)
        image = tf.cond(resize_crit < 1,
                      lambda: tf.image.resize(bigimage, [w*tw/w, h*tw/w]), # if true
                      lambda: tf.image.resize(bigimage, [w*th/h, h*th/h])  # if false
                     )

        nw = tf.shape(image)[0]
        nh = tf.shape(image)[1]
        image = tf.image.crop_to_bounding_box(image, (nw - tw) // 2, (nh - th) // 2, tw, th)
        # image = tf.cast(image, tf.uint8) #/ 255.0

    return image, w, h, bigimage

##========================================================
def fromhex(n):
    """ hexadecimal to integer """
    return int(n, base=16)

##========================================================
def label_to_colors(
    img,
    mask,
    alpha,#=128,
    colormap,#=class_label_colormap, #px.colors.qualitative.G10,
    color_class_offset,#=0,
    do_alpha,#=True
):
    """
    Take MxN matrix containing integers representing labels and return an MxNx4
    matrix where each label has been replaced by a color looked up in colormap.
    colormap entries must be strings like plotly.express style colormaps.
    alpha is the value of the 4th channel
    color_class_offset allows adding a value to the color class index to force
    use of a particular range of colors in the colormap. This is useful for
    example if 0 means 'no class' but we want the color of class 1 to be
    colormap[0].
    """


    colormap = [
        tuple([fromhex(h[s : s + 2]) for s in range(0, len(h), 2)])
        for h in [c.replace("#", "") for c in colormap]
    ]

    cimg = np.zeros(img.shape[:2] + (3,), dtype="uint8")
    minc = np.min(img)
    maxc = np.max(img)

    for c in range(minc, maxc + 1):
        cimg[img == c] = colormap[(c + color_class_offset) % len(colormap)]

    cimg[mask==1] = (0,0,0)

    if do_alpha is True:
        return np.concatenate(
            (cimg, alpha * np.ones(img.shape[:2] + (1,), dtype="uint8")), axis=2
        )
    else:
        return cimg


#====================================================

root = Tk()
root.filename =  filedialog.askopenfilename(initialdir = "/model_training/weights",title = "Select file",filetypes = (("weights file","*.h5"),("all files","*.*")))
weights = root.filename
print(weights)
root.withdraw()

root = Tk()
root.filename =  filedialog.askdirectory(initialdir = "/samples",title = "Select directory of images to segment")
sample_direc = root.filename
print(sample_direc)
root.withdraw()


configfile = weights.replace('.h5','.json').replace('weights', 'config')

with open(configfile) as f:
    config = json.load(f)

for k in config.keys():
    exec(k+'=config["'+k+'"]')


try:
	os.mkdir(sample_direc+os.sep+'masks')
	os.mkdir(sample_direc+os.sep+'masked')
	os.mkdir(sample_direc+os.sep+'conf_var')
	os.mkdir(sample_direc+os.sep+'probs')

except:
	pass

#=======================================================
if USE_LOCATION:
    model = res_unet((TARGET_SIZE[0], TARGET_SIZE[1], N_DATA_BANDS+1), BATCH_SIZE, NCLASSES)
else:
    model = res_unet((TARGET_SIZE[0], TARGET_SIZE[1], N_DATA_BANDS), BATCH_SIZE, NCLASSES)

model.compile(optimizer = 'adam', loss = 'categorical_crossentropy', metrics = [mean_iou, dice_coef])

model.load_weights(weights)


### predict
print('.....................................')
print('Using model for prediction on images ...')

sample_filenames = sorted(tf.io.gfile.glob(sample_direc+os.sep+'*.jpg'))
if len(sample_filenames)==0:
    sample_filenames = sorted(tf.io.gfile.glob(sample_direc+os.sep+'*.png'))

print('Number of samples: %i' % (len(sample_filenames)))

for counter,f in enumerate(sample_filenames):

    if NCLASSES==1:
        if 'jpg' in f:
            segfile = f.replace('.jpg', '_predseg.png')
        elif 'png' in f:
            segfile = f.replace('.png', '_predseg.png')

        segfile = os.path.normpath(segfile)
        segfile = segfile.replace(os.path.normpath(sample_direc), os.path.normpath(sample_direc+os.sep+'masks'))
        if os.path.exists(segfile):
            print('%s exists ... skipping' % (segfile))
            continue
        else:
            print('%s does not exist ... creating' % (segfile))

            start = time.time()

            if N_DATA_BANDS<=3:
                image, w, h, bigimage = seg_file2tensor_3band(f, resize=True)
                image = image#/255
                bigimage = bigimage#/255
                w = w.numpy(); h = h.numpy()
            else:
                image, w, h, bigimage = seg_file2tensor_4band(f, f.replace('aug_images', 'aug_nir'), resize=True )
                image = image#/255
                bigimage = bigimage#/255
                w = w.numpy(); h = h.numpy()

            print("Working on %i x %i image" % (w,h))

            #image = tf.image.per_image_standardization(image)
            image = standardize(image.numpy())

            E = []; W = []
            E.append(model.predict(tf.expand_dims(image, 0) , batch_size=1).squeeze())
            W.append(1)
            # E.append(np.fliplr(model.predict(tf.expand_dims(np.fliplr(image), 0) , batch_size=1).squeeze()))
            # W.append(.75)
            # E.append(np.flipud(model.predict(tf.expand_dims(np.flipud(image), 0) , batch_size=1).squeeze()))
            # W.append(.75)

            for k in np.linspace(100,int(TARGET_SIZE[0]),10):
                #E.append(np.roll(model.predict(tf.expand_dims(np.roll(image, int(k)), 0) , batch_size=1).squeeze(), -int(k)))
                E.append(model.predict(tf.expand_dims(np.roll(image, int(k)), 0) , batch_size=1).squeeze())
                W.append(2*(1/np.sqrt(k)))

            for k in np.linspace(100,int(TARGET_SIZE[0]),10):
                #E.append(np.roll(model.predict(tf.expand_dims(np.roll(image, -int(k)), 0) , batch_size=1).squeeze(), int(k)))
                E.append(model.predict(tf.expand_dims(np.roll(image, -int(k)), 0) , batch_size=1).squeeze())
                W.append(2*(1/np.sqrt(k)))

            K.clear_session()

            #E = [maximum_filter(resize(e,(w,h)), int(w/200)) for e in E]
            E = [resize(e,(w,h)) for e in E]

            #est_label = np.median(np.dstack(E), axis=-1)
            est_label = np.average(np.dstack(E), axis=-1, weights=np.array(W))

            var = np.std(np.dstack(E), axis=-1)

            if 'jpg' in f:
                outfile = os.path.normpath(f.replace('.jpg', '_prob.npz'))
            else:
                outfile = os.path.normpath(f.replace('.png', '_prob.npz'))

            outfile = outfile.replace(os.path.normpath(sample_direc), os.path.normpath(sample_direc+os.sep+'probs'))
            np.savez(outfile, est_label.astype(np.float16))

            if 'jpg' in f:
                outfile = os.path.normpath(f.replace('.jpg', '_prob.tif'))
            else:
                outfile = os.path.normpath(f.replace('.png', '_prob.tif'))

            outfile = outfile.replace(os.path.normpath(sample_direc), os.path.normpath(sample_direc+os.sep+'probs'))
            imsave(outfile, np.dstack((bigimage, 255*est_label)))

            if np.max(est_label)-np.min(est_label) > .5:
                thres = threshold_otsu(est_label)
                print("Otsu threshold: %f" % (thres))
                if thres>.75:
                    thres = .75
            else:
                thres = .75
                print("Default threshold: %f" % (thres))

            conf = 1-est_label
            conf[est_label<thres] = est_label[est_label<thres]
            conf = 1-conf

            conf[np.isnan(conf)] = 0
            conf[np.isinf(conf)] = 0

            model_conf = np.sum(conf)/np.prod(conf.shape)
            print('Overall model confidence = %f'%(model_conf))

            est_label[est_label<thres] = 0
            est_label[est_label>thres] = 1
            est_label = remove_small_holes(est_label.astype('uint8')*2, 2*w)
            est_label = remove_small_objects(est_label.astype('uint8')*2, 2*w)
            est_label[est_label<thres] = 0
            est_label[est_label>thres] = 1
            est_label = np.squeeze(est_label[:w,:h])

            elapsed = (time.time() - start)/60
            print("Image masking took "+ str(elapsed) + " minutes")
            start = time.time()

            imsave(segfile, (est_label*255).astype(np.uint8), check_contrast=False)

            if 'jpg' in f:
                outfile = os.path.normpath(f.replace('.jpg', '_conf.npz'))
            else:
                outfile = os.path.normpath(f.replace('.png', '_conf.npz'))

            outfile = outfile.replace(os.path.normpath(sample_direc), os.path.normpath(sample_direc+os.sep+'conf_var'))
            np.savez(outfile, conf.astype(np.float16))

            if 'jpg' in f:
                outfile = os.path.normpath(f.replace('.jpg', '_var.npz'))
            else:
                outfile = os.path.normpath(f.replace('.png', '_var.npz'))

            outfile = outfile.replace(os.path.normpath(sample_direc), os.path.normpath(sample_direc+os.sep+'conf_var'))
            np.savez(outfile, var.astype(np.float16))

            if 'jpg' in f:
                outfile = os.path.normpath(f.replace('.jpg', '_segoverlay.tif'))
            else:
                outfile = os.path.normpath(f.replace('.png', '_segoverlay.tif'))

            outfile = outfile.replace(os.path.normpath(sample_direc), os.path.normpath(sample_direc+os.sep+'masked'))
            try:
                imsave(outfile, np.dstack((bigimage, (255*est_label))), check_contrast=False)
            except:
                bigimage = bigimage.numpy().squeeze()
                if np.ndim(bigimage)!=3:
                    bigimage = np.dstack((bigimage,bigimage,bigimage))
                imsave(outfile, np.dstack((bigimage, (255*est_label))), check_contrast=False)

            elapsed = (time.time() - start)/60
            print("File writing took "+ str(elapsed) + " minutes")
            print("%s done" % (f))

    else: ###NCLASSES>1

        if 'jpg' in f:
            segfile = f.replace('.jpg', '_predseg.png')
        elif 'png' in f:
            segfile = f.replace('.png', '_predseg.png')

        segfile = os.path.normpath(segfile)
        segfile = segfile.replace(os.path.normpath(sample_direc), os.path.normpath(sample_direc+os.sep+'masks'))
        if os.path.exists(segfile):
            print('%s exists ... skipping' % (segfile))
            continue
        else:
            print('%s does not exist ... creating' % (segfile))

            start = time.time()

            if N_DATA_BANDS<=3:
                image, w, h, bigimage = seg_file2tensor_3band(f, resize=True)
                image = image#/255
                bigimage = bigimage#/255
                w = w.numpy(); h = h.numpy()
            else:
                image, w, h, bigimage = seg_file2tensor_4band(f, f.replace('aug_images', 'aug_nir'), resize=True )
                image = image#/255
                bigimage = bigimage#/255
                w = w.numpy(); h = h.numpy()

            print("Working on %i x %i image" % (w,h))

            #image = tf.image.per_image_standardization(image)
            image = standardize(image.numpy())

            est_label = model.predict(tf.expand_dims(image, 0) , batch_size=1).squeeze()
            K.clear_session()

            E = [maximum_filter(est_label[:,:,k], int(w/2/NCLASSES)) for k in range(NCLASSES)]
            est_label = np.dstack(E)

            est_label = resize(est_label,(w,h))
            conf = np.max(est_label, -1)
            conf[np.isnan(conf)] = 0
            conf[np.isinf(conf)] = 0
            est_label = np.argmax(est_label,-1)

            est_label = np.squeeze(est_label[:w,:h])

            class_label_colormap = ['#3366CC','#DC3912','#FF9900','#109618','#990099','#0099C6','#DD4477','#66AA00','#B82E2E', '#316395'][:NCLASSES]
            class_label_colormap = class_label_colormap[:NCLASSES]
            try:
                color_label = label_to_colors(est_label, bigimage.numpy()[:,:,0]==0, alpha=128, colormap=class_label_colormap, color_class_offset=0, do_alpha=False)
            except:
                color_label = label_to_colors(est_label, bigimage[:,:,0]==0, alpha=128, colormap=class_label_colormap, color_class_offset=0, do_alpha=False)

            elapsed = (time.time() - start)/60
            print("Image masking took "+ str(elapsed) + " minutes")
            start = time.time()

            imsave(segfile, est_label.astype(np.uint8), check_contrast=False)
            #np.savez(f.replace('.jpg', '_conf.npz').replace(sample_direc, sample_direc+os.sep+'conf_var'), conf.astype(np.float16))
            #imsave(f.replace('.jpg', '_segoverlay.png').replace(sample_direc, sample_direc+os.sep+'masked'), np.dstack((255*bigimage.numpy(), (est_label*255))), check_contrast=False)

            if 'jpg' in f:
                outfile = os.path.normpath(f.replace('.jpg', '_conf.npz'))
            else:
                outfile = os.path.normpath(f.replace('.png', '_conf.npz'))

            outfile = outfile.replace(os.path.normpath(sample_direc), os.path.normpath(sample_direc+os.sep+'conf_var'))
            np.savez(outfile, conf.astype(np.float16))

            if 'jpg' in f:
                outfile = f.replace('.jpg', '_predseg_col.png')
                outfile = outfile.replace(os.path.normpath(sample_direc), os.path.normpath(sample_direc+os.sep+'masked'))
                imsave(outfile, (color_label).astype(np.uint8), check_contrast=False)
            elif 'png' in f:
                outfile = f.replace('.png', '_predseg_col.png')
                outfile = outfile.replace(os.path.normpath(sample_direc), os.path.normpath(sample_direc+os.sep+'masked'))
                imsave(outfile, (color_label).astype(np.uint8), check_contrast=False)

            elapsed = (time.time() - start)/60
            print("File writing took "+ str(elapsed) + " minutes")
            print("%s done" % (f))
