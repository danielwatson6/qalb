"""Testing setup for QALB shared task."""

import io
import os
import re
import timeit

import tensorflow as tf
import editdistance
from scipy import exp
from scipy.special import lambertw

from ai.datasets import QALB
from ai.models import Seq2Seq


# HYPERPARAMETERS
tf.app.flags.DEFINE_float('lr', 5e-4, "Initial learning rate.")
tf.app.flags.DEFINE_integer('batch_size', 128, "Batch size.")
tf.app.flags.DEFINE_integer('embedding_size', 128, "Embedding dimensionality.")
tf.app.flags.DEFINE_integer('hidden_size', 256, "Number of hidden units.")
tf.app.flags.DEFINE_integer('rnn_layers', 2, "Number of RNN layers.")
tf.app.flags.DEFINE_boolean('bidirectional_encoder', True, "Whether to use a"
                            " bidirectional RNN in the encoder's 1st layer.")
tf.app.flags.DEFINE_string('bidirectional_mode', 'add', "Set to 'add',"
                           " 'concat' or 'project'.")
tf.app.flags.DEFINE_boolean('use_lstm', False, "Set to False to use GRUs.")
tf.app.flags.DEFINE_string('attention', 'luong', "'bahdanau' or 'luong'"
                           " (default is 'luong').")
tf.app.flags.DEFINE_float('dropout', .6, "Keep probability for dropout on the"
                          "RNNs' non-recurrent connections.")
tf.app.flags.DEFINE_float('max_grad_norm', 10., "Clip gradients to this norm.")
tf.app.flags.DEFINE_integer('beam_size', 5, "Beam search size.")
tf.app.flags.DEFINE_float('initial_p_sample', .35, "Initial decoder sampling"
                          " probability (0=ground truth, 1=use predictions).")
tf.app.flags.DEFINE_float('final_p_sample', .35, "Final decoder sampling"
                          " probability (0=ground truth, 1=use predictions).")
tf.app.flags.DEFINE_integer('epochs_p_sample', 20, "Duration in epochs of"
                            " schedule sampling (determines rate of change).")
tf.app.flags.DEFINE_boolean('linear_p_sample', True, "False = sigmoid decay.")
tf.app.flags.DEFINE_integer('parse_repeated', 0, "Set to > 1 to compress"
                            " contiguous patterns in the data pipeline.")
tf.app.flags.DEFINE_float('epsilon', 1e-8, "Denominator constant.")
tf.app.flags.DEFINE_float('beta1', .9, "First order moment decay.")
tf.app.flags.DEFINE_float('beta2', .999, "Second order moment decay.")

# CONFIG
tf.app.flags.DEFINE_integer('max_sentence_length', 400, "Max. word length of"
                            " training examples (both inputs and labels).")
tf.app.flags.DEFINE_integer('num_steps_per_eval', 50, "Number of steps to wait"
                            " before running the graph with the dev set.")
tf.app.flags.DEFINE_integer('max_epochs', 30, "Number of epochs to run"
                            " (0 = no limit).")
tf.app.flags.DEFINE_string('extension', 'mada.kmle', "Data files' extension.")
tf.app.flags.DEFINE_string('decode', None, "Set to a path to run on a file.")
tf.app.flags.DEFINE_string('output_path', os.path.join('output', 'result.txt'),
                           "Name of the output file with decoding results.")
tf.app.flags.DEFINE_boolean('restore', True, "Whether to restore the model.")
tf.app.flags.DEFINE_string('model_name', None, "Name of the output directory.")


FLAGS = tf.app.flags.FLAGS


def untokenize_batch(dataset, id_batch):
  """Return the UTF-8 sequences of the given batch of ids."""
  return [dataset.untokenize(dataset.clean(s)) for s in id_batch]


def levenshtein(proposed, gold, normalize=False):
  """Return the normalized Levenshtein distance of the given strings."""
  lev_densities = []
  for x, y in zip(proposed, gold):
    score = editdistance.eval(x, y)
    if normalize:
      score /= len(y)
    lev_densities.append(score)
  return sum(lev_densities) / len(lev_densities)


def train():
  """Run a loop that continuously trains the model."""
  print("Building dynamic character-level QALB data...", flush=True)
  dataset = QALB(
    'QALB', parse_repeated=FLAGS.parse_repeated, extension=FLAGS.extension,
    shuffle=True, max_input_length=FLAGS.max_sentence_length,
    max_label_length=FLAGS.max_sentence_length)
  
  print("Building computational graph...", flush=True)
  graph = tf.Graph()
  
  with graph.as_default():
    
    # During training we use beam width 1. There are lots of complications on
    # the implementation, e.g. only tiling during inference.
    m = Seq2Seq(
      num_types=dataset.num_types(),
      max_encoder_length=FLAGS.max_sentence_length,
      max_decoder_length=FLAGS.max_sentence_length,
      pad_id=dataset.type_to_ix['_PAD'],
      eos_id=dataset.type_to_ix['_EOS'],
      go_id=dataset.type_to_ix['_GO'],
      space_id=dataset.type_to_ix[(' ',)],
      ix_to_type=dataset.ix_to_type,
      batch_size=FLAGS.batch_size, embedding_size=FLAGS.embedding_size,
      hidden_size=FLAGS.hidden_size, rnn_layers=FLAGS.rnn_layers,
      bidirectional_encoder=FLAGS.bidirectional_encoder,
      bidirectional_mode=FLAGS.bidirectional_mode,
      use_lstm=FLAGS.use_lstm, attention=FLAGS.attention, 
      dropout=FLAGS.dropout, max_grad_norm=FLAGS.max_grad_norm, beam_size=1,
      epsilon=FLAGS.epsilon, beta1=FLAGS.beta1, beta2=FLAGS.beta2,
      restore=FLAGS.restore, model_name=FLAGS.model_name)
  
  # Allow TensorFlow to resort back to CPU when we try to set an operation to
  # a GPU where there's only a CPU implementation, rather than crashing.
  sess_config = tf.ConfigProto(allow_soft_placement=True)
  
  with tf.Session(graph=graph, config=sess_config) as sess:
    print("Initializing or restoring model...", flush=True)
    m.start()
    
    # If the model was not restored, initialize the variable hyperparameters.
    if sess.run(m.lr) == 0:
      sess.run(tf.assign(m.lr, FLAGS.lr))
    if sess.run(m.p_sample) == 0:
      sess.run(tf.assign(m.p_sample, FLAGS.initial_p_sample))
    
    # Get the number of epochs that have passed (easier by getting batches now)
    step = m.global_step.eval()
    batches = dataset.get_train_batches(m.batch_size)
    epoch = step // len(batches)
    
    # Scheduled sampling decay
    i = FLAGS.initial_p_sample
    f = FLAGS.final_p_sample
    # The stopping point is based on the max epochs
    total_train_steps = len(batches) * FLAGS.epochs_p_sample
    if i != f and not FLAGS.linear_p_sample:
      k = total_train_steps / (float(lambertw(total_train_steps / 2)) * 2)
      expk = float(exp(-total_train_steps / k))
      delta_f = (f - i) * (1 + k) * (1 + k * expk) / (k - k * expk) - f
      delta_i = (f + delta_f) / (1 + k)
    
    while not FLAGS.max_epochs or epoch <= FLAGS.max_epochs:
      print("=====EPOCH {}=====".format(epoch), flush=True)
      while step < (epoch + 1) * len(batches):
        step = m.global_step.eval()
        
        # Scheduled sampling decay
        if i != f:
          # Linear decay
          if FLAGS.linear_p_sample:
            p = min(f, i + step * (f - i) / total_train_steps)
          # Inverse sigmoid decay
          else:
            expk = float(exp(-step / k))
            p = min(f, i - delta_i + (f + delta_f) / (1 + k * expk))
          
          sess.run(tf.assign(m.p_sample, p))
        
        # Gradient descent and backprop
        train_inputs, train_labels = zip(*batches[step % len(batches)])
        train_fd = {m.inputs: train_inputs, m.labels: train_labels}
        
        # Wrap into function to measure running time
        def train_step():
          sess.run(m.train_step, feed_dict=train_fd)
        
        print("Global step {0} ({1}s)".format(
          step, timeit.timeit(train_step, number=1)), flush=True)
        
        if step % FLAGS.num_steps_per_eval == 0:
          valid_inputs, valid_labels = dataset.get_valid_batch(m.batch_size)
          valid_fd = {m.inputs: valid_inputs, m.labels: valid_labels}
          
          # Run training and validation perplexity and samples
          
          lr, train_ppx, train_output, p_sample, train_ppx_summ = sess.run([
            m.lr,
            m.perplexity,
            m.output,
            m.p_sample,
            m.perplexity_summary,
          ], feed_dict=train_fd)
          
          valid_ppx, valid_output, infer_output, valid_ppx_summ = sess.run([
            m.perplexity,
            m.output,
            m.generative_output,
            m.perplexity_summary,
          ], feed_dict=valid_fd)
          
          # Convert data to UTF-8 strings for evaluation and display
          valid_inputs = untokenize_batch(dataset, valid_inputs)
          valid_labels = untokenize_batch(dataset, valid_labels)
          valid_output = untokenize_batch(dataset, valid_output)
          infer_output = untokenize_batch(dataset, infer_output)
          
          # Run evaluation metrics
          lev = levenshtein(infer_output, valid_labels)
          lev_density = levenshtein(infer_output, valid_labels, normalize=True)
          
          lev_summ = sess.run(
            m.lev_summary, feed_dict={m.lev: lev})
          lev_density_summ = sess.run(
            m.lev_density_summary, feed_dict={m.lev_density: lev_density})
          
          # Write summaries to TensorBoard
          m.train_writer.add_summary(train_ppx_summ, global_step=step)
          m.valid_writer.add_summary(valid_ppx_summ, global_step=step)
          m.valid_writer.add_summary(lev_summ, global_step=step)
          m.valid_writer.add_summary(lev_density_summ, global_step=step)
          
          # Display results to stdout
          print("  lr:", lr)
          print("  p_sample:", p_sample)
          print("  train_ppx:", train_ppx)
          print("  valid_ppx:", valid_ppx)
          print("  lev:", lev)
          print("  lev_density:", lev_density)
          print("Input:")
          print(valid_inputs[0])
          print("Target:")
          print(valid_labels[0])
          print("Output with ground truth:")
          print(valid_output[0])
          print("Greedily decoded output:")
          print(infer_output[0], flush=True)
      
      # Epoch about to be done - save, reshuffle the data and get new batches
      print("Saving model...")
      m.save()
      print("Model saved. Resuming training...", flush=True)
      batches = dataset.get_train_batches(m.batch_size)
      epoch += 1


def decode():
  """Run a blind test on the file with path given by the `decode` flag."""
  with io.open(FLAGS.decode, encoding='utf-8') as test_file:
    lines = test_file.readlines()
    # Get the largest sentence length to set an upper bound to the decoder.
    max_length = max([len(line) for line in lines])
    
  print("Building dynamic character-level QALB data...", flush=True)
  dataset = QALB(
    'QALB', parse_repeated=FLAGS.parse_repeated, extension=FLAGS.extension,
    max_input_length=max_length, max_label_length=max_length)
  
  print("Building computational graph...", flush=True)
  graph = tf.Graph()
  with graph.as_default():
    
    m = Seq2Seq(
      num_types=dataset.num_types(),
      max_encoder_length=max_length, max_decoder_length=max_length,
      pad_id=dataset.type_to_ix['_PAD'],
      eos_id=dataset.type_to_ix['_EOS'],
      go_id=dataset.type_to_ix['_GO'],
      space_id=dataset.type_to_ix[(' ',)],
      ix_to_type=dataset.ix_to_type,
      batch_size=1, embedding_size=FLAGS.embedding_size,
      hidden_size=FLAGS.hidden_size, rnn_layers=FLAGS.rnn_layers,
      bidirectional_encoder=FLAGS.bidirectional_encoder,
      bidirectional_mode=FLAGS.bidirectional_mode,
      use_lstm=FLAGS.use_lstm, attention=FLAGS.attention,
      beam_size=FLAGS.beam_size, restore=True, model_name=FLAGS.model_name)
  
  with tf.Session(graph=graph) as sess:
    print("Restoring model...", flush=True)
    m.start()
    print(
      "Restored model (global step {})".format(m.global_step.eval()),
      flush=True)
    with io.open(FLAGS.output_path, 'w', encoding='utf-8') as output_file:
      for line in lines:
        ids = dataset.tokenize(line)
        while len(ids) < max_length:
          ids.append(dataset.type_to_ix['_PAD'])
        outputs = sess.run(m.generative_output, feed_dict={m.inputs: [ids]})
        top_line = untokenize_batch(dataset, outputs)[0]
        # Sequences of text will only be repeated up to 5 times.
        top_line = re.sub(r'(.+?)\1{5,}', lambda m: m.group(1) * 5, top_line)
        output_file.write(top_line + '\n')
        print(top_line, flush=True)        


def main(_):
  """Callee of `tf.app.run` the method."""
  if not FLAGS.model_name:
    raise ValueError(
      "Undefined model name. Perhaps you forgot to set the --model_name flag?")
  
  if FLAGS.decode:
    decode()
  else:
    train()


if __name__ == '__main__':
  tf.app.run()
