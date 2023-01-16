# # cycleGANs for STEM images
#
# In this section, we'll train a GAN to noise up a simulated image to look like an experimental image

# ## Imports

# In[ ]:

import sys

from make_dataset import parse_and_save_dir, load_train_data
from models import unet_generator, discriminator, generator_resnet
import tensorflow as tf
from plotting_tools import *
import pickle
import time
from glob import glob
from IPython.display import clear_output
import scipy.fftpack as sfft
from sys import argv

print(sys.version)
print("Num GPUs Available: ", len(tf.config.list_physical_devices('GPU')))


# ## Loading in Images
# First, we load in large simulated image and experimental images, and cut them into 256x256 chunks. We then store the images in folders.

# In[ ]:

exp_dir_fn = argv[1]
idfn       = argv[2]
gaussian   = 0.1 if len(argv) < 4 else float(argv[3])

parent_dir = "/mnt/d/stem-learning/"
# WSe
exp_dir_raw = parent_dir + "data/WSe/data_for_gan/experiment/{}/".format(exp_dir_fn)
sim_dir_raw = parent_dir + "data/WSe/data_for_gan/simulation/sim_pristine/"
# STO/Gr
#exp_dir_raw = parent_dir + "data/{}/{}_{}/exp_1K/".format(exp_dir_fn, exp_dir_fn, idfn)
#sim_dir_raw = parent_dir + "data/{}/{}_{}/sim_1K/".format(exp_dir_fn, exp_dir_fn, idfn)




exp_dir = exp_dir_raw[:-1] + "_256_slices/"
sim_dir = sim_dir_raw[:-1] + "_gauss_{}_256_slices/".format(gaussian)

print("raw directories:")
print(exp_dir_raw)
print(sim_dir_raw)
print("256 directories:")
print(exp_dir)
print(sim_dir)


fine_size, stride = 256, 8
num_channels=1

lr0 = 2e-3
lam_cycle = 10
lam_ident = 0.5*lam_cycle

identifier = "20230102_MODEL_unet_ident_gen_fft_{}_SIM_pristine_gaussian_{}_EXP_{}_{}".format(lam_cycle, gaussian, exp_dir_fn, idfn)


print("parsing directories")
#parse_and_save_dir(exp_dir_raw, exp_dir, fine_size, stride)
#parse_and_save_dir(sim_dir_raw, sim_dir, fine_size, stride, num_channels=num_channels, gaussian=gaussian)


# ## Construct Generator and Discriminator
# Here, we can choose between a unet generator or a resnet generator.

generator_exp = unet_generator(num_channels, 1, "instancenorm")
generator_sim = unet_generator(1, num_channels, "instancenorm")

discriminator_sim = discriminator("instancenorm", num_channels)
discriminator_exp = discriminator("instancenorm", 1)

discriminator_sim_fft = discriminator("instancenorm", num_channels)
discriminator_exp_fft = discriminator("instancenorm", 1)


generator_exp_optimizer = tf.keras.optimizers.Adam(lr0)
generator_sim_optimizer = tf.keras.optimizers.Adam(lr0)

discriminator_sim_optimizer = tf.keras.optimizers.Adam(lr0)
discriminator_exp_optimizer = tf.keras.optimizers.Adam(lr0)

discriminator_sim_fft_optimizer = tf.keras.optimizers.Adam(lr0)
discriminator_exp_fft_optimizer = tf.keras.optimizers.Adam(lr0)


# ## Loss Functions and Accuracies

#loss_obj = tf.keras.losses.BinaryCrossentropy(from_logits=True)
loss_obj = tf.keras.losses.MeanSquaredError()

def discriminator_loss(real, fake):
    real_loss = loss_obj( tf.ones_like(real), real)
    fake_loss = loss_obj(tf.zeros_like(fake), fake)
    total_disc_loss = (real_loss*len(real) + fake_loss*len(fake))/(len(real) + len(fake))
    return total_disc_loss

def generator_loss(fake):
    return loss_obj(tf.ones_like(fake), fake)

def L1_loss(img_A, img_B):
    return tf.reduce_mean(tf.abs(img_A - img_B))

def L2_loss(img_A, img_B):
    diff = tf.abs(img_A - img_B)
    return tf.reduce_mean(diff*diff)


# ## Checkpoints
checkpoint_path = parent_dir + "cycle_gan_results/checkpoints/checkpoint_{}".format(identifier)
log_data_fn = "{}/data_{}.p".format(checkpoint_path, identifier)

ckpt = tf.train.Checkpoint(generator_exp=generator_exp,
                           generator_sim=generator_sim,
                           discriminator_sim=discriminator_sim,
                           discriminator_exp=discriminator_exp,
                           discriminator_sim_fft=discriminator_sim_fft,
                           discriminator_exp_fft=discriminator_exp_fft,
                           generator_exp_optimizer=generator_exp_optimizer,
                           generator_sim_optimizer=generator_sim_optimizer,
                           discriminator_sim_optimizer=discriminator_sim_optimizer,
                           discriminator_exp_optimizer=discriminator_exp_optimizer,
                           discriminator_sim_fft_optimizer=discriminator_sim_fft_optimizer,
                           discriminator_exp_fft_optimizer=discriminator_exp_fft_optimizer)

ckpt_manager = tf.train.CheckpointManager(ckpt, checkpoint_path, max_to_keep=3)

# if a checkpoint exists, restore the latest checkpoint.
if ckpt_manager.latest_checkpoint:
    #cpath = ckpt_manager.checkpoints[-2]
    cpath = ckpt_manager.latest_checkpoint
    print("loading checkpoint ", cpath)
    ckpt.restore(cpath)
    print ('Latest checkpoint restored!')


# ## Training Functions
@tf.function
def get_logf2(img_arr):
    fft_list = tf.signal.fft2d(tf.cast(tf.reshape(img_arr, [-1, fine_size, fine_size]), tf.complex64))
    re_list, im_list = tf.math.real(fft_list), tf.math.imag(fft_list)
    f2_list = tf.math.multiply(re_list, re_list) + tf.math.multiply(im_list, im_list)
    return tf.reshape(tf.math.log(tf.clip_by_value(f2_list, 1e-36, 1e36))/tf.math.log(10.), [-1, fine_size, fine_size, 1])


@tf.function
def train_generators(real_sim, real_exp, train=True):
    # persistent is set to True because the tape is used more than
    # once to calculate the gradients.
    with tf.GradientTape(persistent=True) as tape:
    # Generator G translates X -> Y
    # Generator F translates Y -> X.

        #Generate fake and cycled images
        fake_exp   = generator_exp(real_sim, training=train)
        cycled_sim = generator_sim(fake_exp, training=train)

        fake_sim   = generator_sim(real_exp, training=train)
        cycled_exp = generator_exp(fake_sim, training=train)

        same_sim   = generator_sim(real_sim, training=train)
        same_exp   = generator_exp(real_exp, training=train)

        #discriminate images
        disc_fake_sim = discriminator_sim(fake_sim, training=False)
        disc_fake_exp = discriminator_exp(fake_exp, training=False)

        #create fft of images
        fake_sim_fft = get_logf2(fake_sim)
        fake_exp_fft = get_logf2(fake_exp)

        #discriminate ffts
        disc_fake_sim_fft = discriminator_sim_fft(fake_sim_fft, training=False)
        disc_fake_exp_fft = discriminator_exp_fft(fake_exp_fft, training=False)

        # calculate the loss
        gen_exp_loss = (generator_loss(disc_fake_exp) + generator_loss(disc_fake_exp_fft))/2
        gen_sim_loss = (generator_loss(disc_fake_sim) + generator_loss(disc_fake_sim_fft))/2

        cycle_exp_loss = L1_loss(real_exp, cycled_exp)
        cycle_sim_loss = L1_loss(real_sim, cycled_sim)
        total_cycle_loss = (cycle_exp_loss + cycle_sim_loss)/2

        # identity loss
        ident_exp_loss = L2_loss(real_exp, same_exp)
        ident_sim_loss = L2_loss(real_sim, same_sim)

        # Total generator loss = adversarial loss + cycle loss
        total_gen_exp_loss = (gen_exp_loss + lam_cycle*total_cycle_loss + lam_ident*ident_exp_loss)/(1 + lam_cycle + lam_ident)
        total_gen_sim_loss = (gen_sim_loss + lam_cycle*total_cycle_loss + lam_ident*ident_sim_loss)/(1 + lam_cycle + lam_ident)

    if train:
        # Calculate the gradients for generator and discriminator
        generator_exp_gradients = tape.gradient(total_gen_exp_loss, generator_exp.trainable_variables)
        generator_sim_gradients = tape.gradient(total_gen_sim_loss, generator_sim.trainable_variables)

        # Apply the gradients to the optimizer
        generator_exp_optimizer.apply_gradients(zip(generator_exp_gradients, generator_exp.trainable_variables))
        generator_sim_optimizer.apply_gradients(zip(generator_sim_gradients, generator_sim.trainable_variables))

    return cycle_exp_loss, cycle_sim_loss, gen_exp_loss, gen_sim_loss, ident_exp_loss, ident_sim_loss

@tf.function
def train_discriminators(real_sim, real_exp, train=True):
    # persistent is set to True because the tape is used more than
    # once to calculate the gradients.
    with tf.GradientTape(persistent=True) as tape:
        # Generator G translates X -> Y
        # Generator F translates Y -> X.

        #generate fake images
        fake_exp   = generator_exp(real_sim, training=False)
        fake_sim   = generator_sim(real_exp, training=False)

        #create ffts
        fake_exp_fft = get_logf2(fake_exp)
        fake_sim_fft = get_logf2(fake_sim)
        real_exp_fft = get_logf2(real_exp)
        real_sim_fft = get_logf2(real_sim)

        #discriminate images
        disc_real_sim = discriminator_sim(real_sim, training=train)
        disc_fake_sim = discriminator_sim(fake_sim, training=train)

        disc_real_exp = discriminator_exp(real_exp, training=train)
        disc_fake_exp = discriminator_exp(fake_exp, training=train)

        #discriminate ffts
        disc_real_sim_fft = discriminator_sim_fft(real_sim_fft, training=train)
        disc_fake_sim_fft = discriminator_sim_fft(fake_sim_fft, training=train)

        disc_real_exp_fft = discriminator_exp_fft(real_exp_fft, training=train)
        disc_fake_exp_fft = discriminator_exp_fft(fake_exp_fft, training=train)

        # Calculate loss
        disc_sim_loss = discriminator_loss(disc_real_sim, disc_fake_sim)
        disc_exp_loss = discriminator_loss(disc_real_exp, disc_fake_exp)

        disc_sim_loss_fft = discriminator_loss(disc_real_sim_fft, disc_fake_sim_fft)
        disc_exp_loss_fft = discriminator_loss(disc_real_exp_fft, disc_fake_exp_fft)

    if train:
        # Calculate the gradients for generator and discriminator
        discriminator_sim_gradients = tape.gradient(disc_sim_loss, discriminator_sim.trainable_variables)
        discriminator_exp_gradients = tape.gradient(disc_exp_loss, discriminator_exp.trainable_variables)

        discriminator_sim_gradients_fft = tape.gradient(disc_sim_loss_fft, discriminator_sim_fft.trainable_variables)
        discriminator_exp_gradients_fft = tape.gradient(disc_exp_loss_fft, discriminator_exp_fft.trainable_variables)

        # Apply the gradients to the optimizer
        discriminator_sim_optimizer.apply_gradients(zip(discriminator_sim_gradients,
                                                        discriminator_sim.trainable_variables))
        discriminator_exp_optimizer.apply_gradients(zip(discriminator_exp_gradients,
                                                        discriminator_exp.trainable_variables))

        discriminator_sim_fft_optimizer.apply_gradients(zip(discriminator_sim_gradients_fft,
                                                            discriminator_sim_fft.trainable_variables))
        discriminator_exp_fft_optimizer.apply_gradients(zip(discriminator_exp_gradients_fft,
                                                            discriminator_exp_fft.trainable_variables))

    return disc_sim_loss, disc_exp_loss, disc_sim_loss_fft, disc_exp_loss_fft


# ## Training

# In[ ]:


gen_exp_losses,   gen_sim_losses   = [], []
disc_exp_losses,  disc_sim_losses  = [], []
disc_exp_fft_losses,  disc_sim_fft_losses  = [], []
cycle_exp_losses, cycle_sim_losses = [], []
dist_gen_exp_losses, dist_gen_sim_losses = [], []
disc_exp_accs,    disc_sim_accs    = [], []
epoch = 0
batch_size = 42
train_size = 100000

data_sim = glob('{}/*.*'.format(sim_dir))
data_exp = glob('{}/*.*'.format(exp_dir))

if len(data_sim) == 0:
    print("ERROR")
    print(sim_dir)
    exit()

if len(data_exp) == 0:
    print("ERROR")
    print(exp_dir)
    exit()


batch_idxs = min(min(len(data_sim), len(data_exp)), train_size) // batch_size

total_epochs = 5e5//(batch_idxs*batch_size)
epoch_step = total_epochs//2

print("batch_idxs = ", batch_idxs)
print("total epochs = ", total_epochs)


try:
    ( gen_exp_losses,   gen_sim_losses,
     disc_exp_losses,  disc_sim_losses,
     disc_exp_fft_losses,  disc_sim_fft_losses,
    cycle_exp_losses, cycle_sim_losses,
    dist_gen_exp_losses, dist_gen_sim_losses,
     disc_exp_accs,    disc_sim_accs,
     epoch) = pickle.load(open(log_data_fn, "rb" ))
    print("loading at epoch ", epoch)
except Exception as e:
    print(e)
    pass
last=None


# In[ ]:




while epoch < total_epochs:
    start = time.time()
    lr = lr0*(1 - (epoch - epoch_step)/(total_epochs-1 - epoch_step)) if epoch >= epoch_step else lr0
    print(epoch, lr)
    generator_exp_optimizer.lr.assign(lr)
    generator_sim_optimizer.lr.assign(lr)
    discriminator_sim_optimizer.lr.assign(lr)
    discriminator_exp_optimizer.lr.assign(lr)
    discriminator_sim_fft_optimizer.lr.assign(lr)
    discriminator_exp_fft_optimizer.lr.assign(lr)

    np.random.shuffle(data_sim)
    np.random.shuffle(data_exp)

    dis_losses, gen_losses = np.zeros(4), np.zeros(6)
    for idx in range(0, batch_idxs):
        batch_start =  time.time()
        sim_fn_list = data_sim[idx*batch_size:(idx + 1)*batch_size]
        exp_fn_list = data_exp[idx*batch_size:(idx + 1)*batch_size]

        real_sim = np.array([load_train_data(fn, num_channels) for fn in sim_fn_list]).astype(np.float32)
        real_exp = np.array([load_train_data(fn, num_channels) for fn in exp_fn_list]).astype(np.float32)

        dis_losses += train_discriminators(real_sim, real_exp, train=True)
        gen_losses += train_generators(real_sim, real_exp, train=True)

        print("[{}/{}]: {}".format(idx, batch_idxs, (time.time()-batch_start)/batch_size))

    clear_output(wait=True)
    cycle_exp_loss, cycle_sim_loss, gen_exp_loss, gen_sim_loss, dist_gen_exp_loss, dist_gen_sim_loss= gen_losses/batch_idxs
    disc_sim_loss, disc_exp_loss, disc_sim_loss_fft, disc_exp_loss_fft = dis_losses/batch_idxs

    # Calculate accuracy
    fake_sim = generator_sim(real_exp, training=False)
    fake_exp = generator_exp(real_sim, training=False)

    disc_sim_acc = get_discriminator_acc(discriminator_sim, real_sim, fake_sim, threshold=0.5)
    disc_exp_acc = get_discriminator_acc(discriminator_exp, real_exp, fake_exp, threshold=0.5)

    gen_exp_losses.append(gen_exp_loss)
    gen_sim_losses.append(gen_sim_loss)
    cycle_exp_losses.append(cycle_exp_loss)
    cycle_sim_losses.append(cycle_sim_loss)
    dist_gen_exp_losses.append(dist_gen_exp_loss)
    dist_gen_sim_losses.append(dist_gen_sim_loss)
    disc_sim_losses.append(disc_sim_loss)
    disc_exp_losses.append(disc_exp_loss)
    disc_sim_fft_losses.append(disc_sim_loss_fft)
    disc_exp_fft_losses.append(disc_exp_loss_fft)
    disc_sim_accs.append(disc_sim_acc)
    disc_exp_accs.append(disc_exp_acc)
    epoch += 1

    sample_sim = real_sim[0].reshape([1,fine_size,fine_size,num_channels])
    sample_exp = real_exp[0].reshape([1,fine_size,fine_size,1])
    generate_images(generator_exp, generator_sim, sample_sim)
    generate_images(generator_sim, generator_exp, sample_exp)
    generate_losses(gen_exp_losses,   gen_sim_losses,
                    cycle_exp_losses, cycle_sim_losses,
                    dist_gen_exp_losses, dist_gen_sim_losses,
                    disc_sim_losses,  disc_exp_losses,   epoch, last=last)
    generate_accuracies(disc_sim_accs, disc_exp_accs, epoch, last=last)

    if (epoch) % 5 == 0:
        ckpt_save_path = ckpt_manager.save()
        print ('Saving checkpoint for epoch {} at {}'.format(epoch, ckpt_save_path))
        pickle.dump( (    gen_exp_losses,   gen_sim_losses,
                         disc_exp_losses,  disc_sim_losses,
                      disc_exp_fft_losses,  disc_sim_fft_losses,
                        cycle_exp_losses, cycle_sim_losses,
                        dist_gen_exp_losses, dist_gen_sim_losses,
                         disc_exp_accs,    disc_sim_accs,
                        epoch-1),
                     open( log_data_fn, "wb" ) )

    print ('Time taken for epoch {} is {} sec\n'.format(epoch, time.time()-start))
