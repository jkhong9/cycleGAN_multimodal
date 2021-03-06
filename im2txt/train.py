# Copyright 2016 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#		 http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Train the model."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import math
import os
import time
import datetime
import numpy as np
import tensorflow as tf

import configuration
import show_and_tell_model
from behavior_generator import BehaviorGenerator
from behavior_discriminator import BehaviorDiscriminator
from inference_utils import vocabulary, caption_generator

import pdb

FLAGS = tf.app.flags.FLAGS

tf.flags.DEFINE_string("input_file_pattern", "",
						"File pattern of sharded TFRecord input files.")
tf.flags.DEFINE_string("inception_checkpoint_file", "",
						"Path to a pretrained inception_v3 model.")
tf.flags.DEFINE_string("train_dir", "",
						"Directory for saving and loading model checkpoints.")
tf.flags.DEFINE_boolean("train_inception", False,
						"Whether to train inception submodel variables.")
tf.flags.DEFINE_integer("number_of_steps", 1000000, "Number of training steps.")
tf.flags.DEFINE_integer("log_every_n_steps", 1,
						"Frequency at which loss and global step are logged.")
tf.flags.DEFINE_string("vocab_file", "", "")

tf.logging.set_verbosity(tf.logging.INFO)


def main(unused_argv):
	assert FLAGS.input_file_pattern, "--input_file_pattern is required"
	assert FLAGS.train_dir, "--train_dir is required"

	model_config = configuration.ModelConfig()
	model_config.input_file_pattern = FLAGS.input_file_pattern
	model_config.inception_checkpoint_file = FLAGS.inception_checkpoint_file
	training_config = configuration.TrainingConfig()

	# Create training directory.
	train_dir = FLAGS.train_dir
	filename_saved_model = os.path.join(FLAGS.train_dir,'im2txt')
	if not tf.gfile.IsDirectory(train_dir):
		tf.logging.info("Creating training directory: %s", train_dir)
		tf.gfile.MakeDirs(train_dir)

	vocab = vocabulary.Vocabulary( FLAGS.vocab_file )

	# Build the TensorFlow graph.
	g = tf.Graph()
	with g.as_default():
		# generator part
		behavior_generator = BehaviorGenerator( model_config, vocab )
		behavior_generator.build()
		NLL_loss = behavior_generator.loss
		global_step = behavior_generator.global_step

		# prepare behavior to be LSTM's input
		teacher_behavior = behavior_generator.teacher_behavior
		free_behavior = behavior_generator.free_behavior
		summary = behavior_generator.summary

		# collect LSTM feature from generator
		generated_text_feature = free_behavior[:-3]

		# discriminator part
		discriminator = BehaviorDiscriminator( model_config )
		discriminator.build( teacher_behavior, free_behavior, behavior_generator.input_mask )
		d_loss = discriminator.d_loss
		g_loss = discriminator.g_loss
		d_accuracy = discriminator.accuracy

		summary.update( discriminator.summary )

		# text2image part
		

		g_and_NLL_loss = g_loss + NLL_loss
		summary.update( {'g_and_NLL_loss':tf.summary.scalar('g_loss+NLL_loss',g_and_NLL_loss)} )

		# Set up the learning rate for training ops
		learning_rate_decay_fn = None
		if FLAGS.train_inception:
			learning_rate = tf.constant(training_config.train_inception_learning_rate)
		else:
			learning_rate = tf.constant(training_config.initial_learning_rate)
			if training_config.learning_rate_decay_factor > 0:
				num_batches_per_epoch = (training_config.num_examples_per_epoch //
																 model_config.batch_size)
				decay_steps = int(num_batches_per_epoch *
													training_config.num_epochs_per_decay)

				def _learning_rate_decay_fn(_learning_rate, _global_step):
					return tf.train.exponential_decay(
							_learning_rate,
							_global_step,
							decay_steps=decay_steps,
							decay_rate=training_config.learning_rate_decay_factor,
							staircase=True)

				learning_rate_decay_fn = _learning_rate_decay_fn

		# Collect trainable variables
		vars_all = [ v for v in tf.trainable_variables() \
								 if v not in behavior_generator.inception_variables ]
		d_vars = [ v for v in vars_all if 'discr' in v.name ]
		g_vars = [ v for v in vars_all if 'discr' not in v.name ]

		# Set up the training ops.
		train_op_NLL = tf.contrib.layers.optimize_loss(
											loss = NLL_loss,
											global_step = global_step,
											learning_rate = learning_rate,
											optimizer = training_config.optimizer,
											clip_gradients = training_config.clip_gradients,
											learning_rate_decay_fn = learning_rate_decay_fn,
											variables = g_vars,
											name='optimize_NLL_loss' )

		train_op_disc = tf.contrib.layers.optimize_loss(
											loss = d_loss,
											global_step = global_step,
											learning_rate = learning_rate,
											optimizer = training_config.optimizer,
											clip_gradients = training_config.clip_gradients,
											learning_rate_decay_fn = learning_rate_decay_fn,
											variables = d_vars,
											name='optimize_disc_loss' )

		train_op_gen = tf.contrib.layers.optimize_loss(
											loss = g_and_NLL_loss,
											global_step=global_step,
											learning_rate=learning_rate,
											optimizer=training_config.optimizer,
											clip_gradients=training_config.clip_gradients,
											learning_rate_decay_fn=learning_rate_decay_fn,
											variables = g_vars,
											name='optimize_gen_loss' )



		# Set up the Saver for saving and restoring model checkpoints.
		saver = tf.train.Saver(max_to_keep=training_config.max_checkpoints_to_keep)

		with tf.Session() as sess:
			# load inception variables
			behavior_generator.init_fn( sess )
			
			# Set up the training ops
			nBatches = num_batches_per_epoch
			
			summaryWriter = tf.summary.FileWriter(train_dir, sess.graph)
			tf.global_variables_initializer().run()
			
			# start input enqueue threads
			coord = tf.train.Coordinator()
			threads = tf.train.start_queue_runners(sess=sess, coord=coord)
			
			counter = 0
			start_time = time.time()
			could_load, checkpoint_counter = load( sess, saver, train_dir )
			if could_load:
				counter = checkpoint_counter

			try:
				# for validation
				with tf.gfile.GFile('data/mscoco/raw-data/val2014/COCO_val2014_000000224477.jpg','r') as f:
					image_valid = f.read()
				f_valid_text = open(os.path.join(train_dir,'valid.txt'),'a')
			
				# run inference for not-trained model
				#self.valid( valid_image, f_valid_text )
				captions = behavior_generator.generate( sess, image_valid )
				f_valid_text.write( 'initial caption {}\n'.format( str(datetime.datetime.now().time())[:-7] ) )
				for i, caption in enumerate(captions):
					sentence = [vocab.id_to_word(w) for w in caption.sentence[1:-1]]
					sentence = " ".join(sentence)
					sentence = "  %d) %s (p=%f)" % (i, sentence, math.exp(caption.logprob))
					print( sentence )
					f_valid_text.write( sentence +'\n' )
				f_valid_text.flush()


				# run training loop
				lossnames_to_print = ['NLL_loss','g_loss', 'd_loss', 'd_acc', 'g_acc']
				val_NLL_loss = float('Inf')
				val_g_loss = float('Inf')
				val_d_loss = float('Inf')
				val_d_acc = 0
				val_g_acc = 0
				for epoch in range(FLAGS.number_of_steps):
					for batch_idx in range(nBatches):
						counter += 1
						is_disc_trained = False
						is_gen_trained = False
						if val_NLL_loss> 3.5:
							_, val_NLL_loss, summary_str = sess.run([train_op_NLL, NLL_loss, summary['NLL_loss']] )
							summaryWriter.add_summary(summary_str, counter)
						else:
							# train discriminator
#							if val_d_acc < 0.9:
#							is_disc_trained = True
							_, val_d_loss, val_d_acc, \
							smr1, smr2, smr3, smr4 = sess.run([train_op_disc, d_loss, d_accuracy, 
								 summary['d_loss_teacher'], summary['d_loss_free'], summary['d_loss'],summary['d_accuracy']] )
							summaryWriter.add_summary(smr1, counter)
							summaryWriter.add_summary(smr2, counter)
							summaryWriter.add_summary(smr3, counter)
							summaryWriter.add_summary(smr4, counter)
						# train generator
#						if val_d_acc > 0.45:
#							is_gen_trained = True	
							# val_g_acc is temporarily named variable instead of val_d_acc
							_, val_g_loss, val_NLL_loss, val_g_acc, smr1, smr2, smr3 = sess.run( 
								[train_op_gen,g_loss,NLL_loss, d_accuracy, 
								summary['g_loss'],summary['NLL_loss'], summary['g_and_NLL_loss']] )
							summaryWriter.add_summary(smr1, counter)
							summaryWriter.add_summary(smr2, counter)
							summaryWriter.add_summary(smr3, counter)
							_, val_g_loss, val_NLL_loss, val_g_acc, smr1, smr2, smr3 = sess.run( 
								[train_op_gen,g_loss,NLL_loss, d_accuracy, 
								summary['g_loss'],summary['NLL_loss'], summary['g_and_NLL_loss']] )
							summaryWriter.add_summary(smr1, counter)
							summaryWriter.add_summary(smr2, counter)
							summaryWriter.add_summary(smr3, counter)
			
						if counter % FLAGS.log_every_n_steps==0:
							elapsed = time.time() - start_time
							log( epoch, batch_idx, nBatches, lossnames_to_print,
								 [val_NLL_loss,val_g_loss,val_d_loss,val_d_acc,val_g_acc], elapsed, counter )
			
#						if is_gen_trained:
#							val_d_acc = val_g_acc

						if counter % 500 == 1 or \
							(epoch==FLAGS.number_of_steps-1 and batch_idx==nBatches-1) :
							saver.save( sess, filename_saved_model, global_step=counter)
			
						if (batch_idx+1) % (nBatches//10) == 0  or batch_idx == nBatches-1:
							# run test after every epoch
							#self.valid( valid_image, f_valid_text )
							captions = behavior_generator.generate_text( sess, image_valid )
							f_valid_text.write( 'count {} epoch {} batch {}/{} ({})\n'.format( \
								counter, epoch, batch_idx, nBatches, str(datetime.datetime.now().time())[:-7] ) )
							for i, caption in enumerate(captions):
								sentence = [vocab.id_to_word(w) for w in caption.sentence[1:-1]]
								sentence = " ".join(sentence)
								sentence = "  %d) %s (p=%f)" % (i, sentence, math.exp(caption.logprob))
								print( sentence )
								f_valid_text.write( sentence +'\n' )
							f_valid_text.flush()

			
			except tf.errors.OutOfRangeError:
				print('Finished training: epoch limit reached')
			finally:
				coord.request_stop()
			coord.join(threads)

def load(sess, saver, checkpoint_dir):
	import re
	print(" [*] Reading checkpoints...")

	ckpt = tf.train.get_checkpoint_state(checkpoint_dir)
	if ckpt and ckpt.model_checkpoint_path:
		ckpt_name = os.path.basename(ckpt.model_checkpoint_path)
		saver.restore(sess, os.path.join(checkpoint_dir, ckpt_name))
		counter = int(next(re.finditer("(\d+)(?!.*\d)",ckpt_name)).group(0))
		print(" [*] Success to read {}".format(ckpt_name))
		return True, counter
	else:
		print(" [*] Failed to find a checkpoint")
		return False, 0

def log( epoch, batch, nBatches, lossnames, losses, elapsed, counter=None, filelogger=None ):
	nDigits = len(str(nBatches))
	str_lossnames = ""
	str_losses = ""
	assert( len(lossnames) == len(losses) )
	isFirst = True
	for lossname, loss in zip(lossnames,losses):
		if not isFirst:
			str_lossnames += ','
			str_losses += ', '
		str_lossnames += lossname
		if type(loss) == str:
			str_losses += loss
		else:
			str_losses += '{:.4f}'.format(loss)
		isFirst = False

	m,s = divmod( elapsed, 60 )
	h,m = divmod( m,60 )
	timestamp = "{:2}:{:02}:{:02}".format( int(h),int(m),int(s) )
	log = "{} e{} b {:>{}}/{} ({})=({})".format( timestamp, epoch, batch, nDigits, nBatches, str_lossnames, str_losses )
	if counter is not None:
		log = "{:>5}_".format(counter) + log
	print( log )
	if filelogger:
		filelogger.write( log )
	return log


if __name__ == "__main__":
	tf.app.run()


