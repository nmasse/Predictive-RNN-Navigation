### Authors: Nicolas Y. Masse, Gregory D. Grant

# Required packages
import tensorflow as tf
import numpy as np
import pickle
import os, sys, time
from itertools import product
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

# Model modules
from parameters import *
import stimulus
import AdamOpt

# Match GPU IDs to nvidia-smi command
os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"

# Ignore Tensorflow startup warnings
os.environ['TF_CPP_MIN_LOG_LEVEL']='2'

stimulus_access = stimulus.RoomStimulus()

class Model:

    """ RNN model for supervised and reinforcement learning training """

    def __init__(self):

        self.time_mask = tf.unstack(tf.ones([par['num_time_steps'],par['batch_size']]), axis=0)

        # Declare all Tensorflow variables
        self.declare_variables()

        # Build the Tensorflow graph
        self.rnn_cell_loop()

        # Train the model
        self.optimize()


    def declare_variables(self):
        """ Initialize all required variables """

        # All the possible prefixes based on network setup
        lstm_var_prefixes   = ['Wf', 'Wi', 'Wo', 'Wc', 'Uf', 'Ui', 'Uo', 'Uc', 'bf', 'bi', 'bo', 'bc', 'W_pred', 'b_pred']
        rl_var_prefixes     = ['W_pol_out', 'b_pol_out', 'W_val_out', 'b_val_out']
        #base_var_prefies    = ['W_out', 'b_out']

        # Add relevant prefixes to variable declaration
        prefix_list = []
        prefix_list += lstm_var_prefixes
        prefix_list += rl_var_prefixes

        # Use prefix list to declare required variables and place them in a dict
        self.var_dict = {}
        with tf.variable_scope('network'):
            for name in lstm_var_prefixes:
                self.var_dict[name] = []
                for i in range(par['num_pred_cells']):
                    self.var_dict[name].append(tf.get_variable(name + str(i), initializer = par[name + '_init'][i]))
            for name in rl_var_prefixes:
                self.var_dict[name] = tf.get_variable(name, initializer = par[name + '_init'])


    def rnn_cell_loop(self):
        """ Initialize parameters and execute loop through
            time to generate the network outputs """

        # Specify training method outputs
        self.input_data = []
        self.output     = []
        self.mask       = []
        self.pol_out    = []
        self.val_out    = []
        self.action     = []
        self.reward     = []
        self.agent_locs = []
        reward = tf.constant(np.zeros((par['batch_size'], par['n_val']), dtype = np.float32))
        action = tf.constant(np.zeros((par['batch_size'], par['n_pol']), dtype = np.float32))
        feedback_reward = tf.constant(np.zeros((par['batch_size'], par['n_val']), dtype = np.float32))

        # Initialize state records
        self.h                  = []
        self.total_pred_error   = [[] for _ in range(par['num_pred_cells'])]
        self.stim_pred_error    = [[] for _ in range(par['num_pred_cells'])]
        self.rew_pred_error     = [[] for _ in range(par['num_pred_cells'])]
        self.act_pred_error     = [[] for _ in range(par['num_pred_cells'])]

        # Initialize network state
        h     = [tf.zeros_like(par['h_init'][i]) for i in range(par['num_pred_cells'])]
        c     = [tf.zeros_like(par['h_init'][i]) for i in range(par['num_pred_cells'])]
        mask  = tf.constant(np.ones((par['batch_size'], 1), dtype = np.float32))

        self.expected_reward_vector = []
        self.actual_reward_vector = []
        # Loop through time, procuring new inputs at the end of each time step
        for t in range(par['num_time_steps']):

            with tf.device('/cpu:0'):
                inputs, = tf.py_func(stimulus_access.make_inputs, [], [tf.float32])
                inputs  = tf.stop_gradient(tf.reshape(inputs, shape=[par['batch_size'], par['n_input']]))
                self.agent_locs.append(tf.py_func(stimulus_access.get_agent_locs, [], [tf.float32]))
            self.input_data.append(inputs)

            # Iterate over sequene of predictive cells
            for i in range(par['num_pred_cells']):
                # Compute the state of the hidden layer
                # x is cell input, y is top-down activity input
                y = None if i == par['num_pred_cells']-1 else h[i+1]
                x = inputs if i == 0 else error_signal

                x = tf.concat([x, reward*i, action*i], axis=-1)
                h[i], c[i], error_signal = self.predictive_cell(x, y, h[i], c[i], i)

                # Determine error signal for each
                es = tf.stack([error_signal[:,:par['n_input']+1+par['n_pol']], \
                               error_signal[:,par['n_input']+1+par['n_pol']:]], axis=-1)
                for ind in range(2):
                    self.stim_pred_error[i].append(tf.reduce_mean(es[:,:par['n_input'],ind]))
                    self.rew_pred_error[i].append(tf.reduce_mean(es[:,par['n_input']:par['n_input']+1,ind]))
                    self.act_pred_error[i].append(tf.reduce_mean(es[:,par['n_input']+1:par['n_input']+10,ind]))
                    self.total_pred_error[i].append(self.stim_pred_error[i][-1] + self.rew_pred_error[i][-1] + self.act_pred_error[i][-1])

                error_signal = tf.concat([es[:,:par['n_input'],0], es[:,:par['n_input'],1]], axis=1)
                error_signal = tf.maximum(error_signal[:,0::2], error_signal[:,1::2])

            self.actual_reward_vector.append(reward)

            # Compute outputs for action
            pol_out        = h[-1] @ self.var_dict['W_pol_out'] + self.var_dict['b_pol_out']
            action_index   = tf.multinomial(pol_out, 1)
            action         = tf.one_hot(tf.squeeze(action_index), par['n_pol'])

            # Compute outputs for loss
            pol_out        = tf.nn.softmax(pol_out, 1)  # Note softmax for entropy loss
            val_out        = h[-1] @ self.var_dict['W_val_out'] + self.var_dict['b_val_out']

            # Check for trial continuation (ends if previous reward was non-zero)
            continue_trial = tf.cast(tf.equal(reward, 0.), tf.float32)
            mask          *= continue_trial

            if t < par['num_time_steps']-2:
                with tf.device('/cpu:0'):
                    feedback_reward, = tf.py_func(stimulus_access.agent_action, [action, mask], [tf.float32])
                    feedback_reward  = tf.stop_gradient(tf.reshape(feedback_reward, shape=[par['batch_size'],1]))
            else:
                feedback_reward = tf.constant(par['failure_penalty'])

            reward = feedback_reward*mask*tf.reshape(self.time_mask[t],[par['batch_size'], 1])

            # Record RL outputs
            self.pol_out.append(pol_out)
            self.val_out.append(val_out)
            self.action.append(action)
            self.reward.append(reward)
            self.h.append(h)

            # Record mask (outside if statement for cross-comptability)
            self.mask.append(mask)

        self.expected_reward_vector = tf.stack(self.expected_reward_vector, axis=0)
        self.actual_reward_vector = tf.stack(self.actual_reward_vector, axis=0)


    def predictive_cell(self, x, y, h, c, cell_num):
        """ Using the appropriate recurrent cell
            architecture, compute the hidden state """

        if cell_num == 1:
            self.expected_reward_vector.append((h @ self.var_dict['W_pred'][cell_num] + self.var_dict['b_pred'][cell_num])[:,par['n_input']:par['n_input']+1])

        pos_err = tf.nn.relu(x - h @ self.var_dict['W_pred'][cell_num] - self.var_dict['b_pred'][cell_num])
        neg_err = tf.nn.relu(h @ self.var_dict['W_pred'][cell_num] + self.var_dict['b_pred'][cell_num] - x)
        error_signal = tf.concat([pos_err, neg_err], axis = -1)
        rnn_input = error_signal if y is None else tf.concat([error_signal, y], axis = -1)

        # Compute LSTM state
        # f : forgetting gate, i : input gate,
        # c : cell state, o : output gate
        f   = tf.sigmoid(rnn_input @ self.var_dict['Wf'][cell_num] + h @ self.var_dict['Uf'][cell_num] + self.var_dict['bf'][cell_num])
        i   = tf.sigmoid(rnn_input @ self.var_dict['Wi'][cell_num] + h @ self.var_dict['Ui'][cell_num] + self.var_dict['bi'][cell_num])
        cn  = tf.tanh(rnn_input @ self.var_dict['Wc'][cell_num] + h @ self.var_dict['Uc'][cell_num] + self.var_dict['bc'][cell_num])
        c   = f * c + i * cn
        o   = tf.sigmoid(rnn_input @ self.var_dict['Wo'][cell_num] + h @ self.var_dict['Uo'][cell_num] + self.var_dict['bo'][cell_num])

        # Compute hidden state
        h = o * tf.tanh(c)

        #error_signal = pos_err + neg_err
        return h, c, error_signal


    def optimize(self):
        """ Calculate losses and apply corrections to model """

        # Set up optimizer and required constants
        epsilon = 1e-7
        adam_optimizer = AdamOpt.AdamOpt(tf.trainable_variables(), learning_rate=par['learning_rate'])

        # Make stabilization records
        self.prev_weights = {}
        self.big_omega_var = {}
        reset_prev_vars_ops = []
        aux_losses = []

        # Set up stabilization based on trainable variables
        for var in tf.trainable_variables():
            n = var.op.name

            # Make big omega and prev_weight variables
            self.big_omega_var[n] = tf.Variable(tf.zeros(var.get_shape()), trainable=False)
            self.prev_weights[n]  = tf.Variable(tf.zeros(var.get_shape()), trainable=False)

            # Don't stabilize value weights/biases
            if not 'val' in n:
                aux_losses.append(par['omega_c'] * \
                    tf.reduce_sum(self.big_omega_var[n] * tf.square(self.prev_weights[n] - var)))

            # Make a reset function for each prev_weight element
            reset_prev_vars_ops.append(tf.assign(self.prev_weights[n], var))

        # Auxiliary stabilization loss
        self.aux_loss = tf.add_n(aux_losses)

        # Spiking activity loss (penalty on high activation values in the hidden layer)
        self.spike_loss = par['spike_cost']*tf.reduce_mean(tf.stack([mask*time_mask*tf.reduce_mean(h) \
            for (h, mask, time_mask) in zip(self.h, self.mask, self.time_mask)]))

        # Training-specific losses
        if par['training_method'] == 'SL':
            RL_loss = tf.constant(0.)

            # Task loss (cross entropy)
            self.pol_loss = tf.reduce_mean([mask*tf.nn.softmax_cross_entropy_with_logits(logits=y, \
                labels=target, dim=1) for y, target, mask in zip(self.output, self.target_data, self.time_mask)])
            sup_loss = self.pol_loss

        elif par['training_method'] == 'RL':
            sup_loss = tf.constant(0.)

            # Collect information from across time
            self.time_mask  = tf.reshape(tf.stack(self.time_mask),(par['num_time_steps'], par['batch_size'], 1))
            self.mask       = tf.stack(self.mask)
            self.reward     = tf.stack(self.reward)
            self.action     = tf.stack(self.action)
            self.pol_out    = tf.stack(self.pol_out)

            # Get the value outputs of the network, and pad the last time step
            val_out = tf.concat([tf.stack(self.val_out), tf.zeros([1,par['batch_size'],par['n_val']])], axis=0)

            # Determine terminal state of the network
            terminal_state = tf.cast(tf.logical_not(tf.equal(self.reward, tf.constant(0.))), tf.float32)

            # Compute predicted value and the advantage for plugging into the policy loss
            pred_val = self.reward + par['discount_rate']*val_out[1:,:,:]*(1-terminal_state)
            advantage = pred_val - val_out[:-1,:,:]

            # Stop gradients back through action, advantage, and mask
            action_static    = tf.stop_gradient(self.action)
            advantage_static = tf.stop_gradient(advantage)
            mask_static      = tf.stop_gradient(self.mask)

            # Policy loss
            self.pol_loss = -tf.reduce_mean(advantage_static*mask_static*self.time_mask*action_static*tf.log(epsilon+self.pol_out))

            # Value loss
            self.val_loss = 0.5*par['val_cost']*tf.reduce_mean(mask_static*self.time_mask*tf.square(val_out[:-1,:,:]-tf.stop_gradient(pred_val)))

            # Entropy loss
            self.entropy_loss = -par['entropy_cost']*tf.reduce_mean(tf.reduce_sum(mask_static*self.time_mask*self.pol_out*tf.log(epsilon+self.pol_out), axis=1))

            # Prediction loss
            self.pred_loss = par['error_cost'] * tf.reduce_mean(tf.stack(self.total_pred_error))

            # Collect RL losses
            RL_loss = self.pol_loss + self.val_loss - self.entropy_loss + self.pred_loss

        # Collect loss terms and compute gradients
        total_loss = sup_loss + RL_loss + self.aux_loss + self.spike_loss
        self.train_op = adam_optimizer.compute_gradients(total_loss)

        # Stabilize weights
        if par['stabilization'] == 'pathint':
            # Zenke method
            self.pathint_stabilization(adam_optimizer)
        elif par['stabilization'] == 'EWC':
            # Kirkpatrick method
            self.EWC()
        else:
            # No stabilization
            pass

        # Make reset operations
        self.reset_prev_vars = tf.group(*reset_prev_vars_ops)
        self.reset_adam_op = adam_optimizer.reset_params()
        self.reset_weights()

        # Make saturation correction operation
        self.make_recurrent_weights_positive()


    def reset_weights(self):
        """ Make new weights, if requested """

        reset_weights = []
        for var in tf.trainable_variables():
            if 'b' in var.op.name:
                # reset biases to 0
                reset_weights.append(tf.assign(var, var*0.))
            elif 'W' in var.op.name:
                # reset weights to initial-like conditions
                new_weight = initialize_weight(var.shape, par['connection_prob'])
                reset_weights.append(tf.assign(var,new_weight))

        self.reset_weights = tf.group(*reset_weights)


    def make_recurrent_weights_positive(self):
        """ Very slightly de-saturate recurrent weights """

        reset_weights = []
        for var in tf.trainable_variables():
            if 'W_rnn' in var.op.name:
                # make all negative weights slightly positive
                reset_weights.append(tf.assign(var,tf.maximum(1e-9, var)))

        self.reset_rnn_weights = tf.group(*reset_weights)


    def pathint_stabilization(self, adam_optimizer):
        """ Synaptic stabilization via the Zenke method """

        # Set up method
        optimizer_task = tf.train.GradientDescentOptimizer(learning_rate =  1.0)
        small_omega_var = {}
        small_omega_var_div = {}

        reset_small_omega_ops = []
        update_small_omega_ops = []
        update_big_omega_ops = []

        # If using reinforcement learning, update rewards
        if par['training_method'] == 'RL':
            self.previous_reward = tf.Variable(-tf.ones([]), trainable=False)
            self.current_reward = tf.Variable(-tf.ones([]), trainable=False)

            reward_stacked = tf.stack(self.reward, axis = 0)
            current_reward = tf.reduce_mean(tf.reduce_sum(reward_stacked, axis = 0))
            self.update_current_reward = tf.assign(self.current_reward, current_reward)
            self.update_previous_reward = tf.assign(self.previous_reward, self.current_reward)

        # Iterate over variables in the model
        for var in tf.trainable_variables():

            # Reset the small omega vars
            small_omega_var[var.op.name] = tf.Variable(tf.zeros(var.get_shape()), trainable=False)
            small_omega_var_div[var.op.name] = tf.Variable(tf.zeros(var.get_shape()), trainable=False)
            reset_small_omega_ops.append(tf.assign(small_omega_var[var.op.name], small_omega_var[var.op.name]*0.0 ) )
            reset_small_omega_ops.append(tf.assign(small_omega_var_div[var.op.name], small_omega_var_div[var.op.name]*0.0 ) )

            # Update the big omega vars based on the training method
            if par['training_method'] == 'RL':
                update_big_omega_ops.append(tf.assign_add( self.big_omega_var[var.op.name], tf.div(tf.abs(small_omega_var[var.op.name]), \
                	(par['omega_xi'] + small_omega_var_div[var.op.name]))))
            elif par['training_method'] == 'SL':
                update_big_omega_ops.append(tf.assign_add( self.big_omega_var[var.op.name], tf.div(tf.nn.relu(small_omega_var[var.op.name]), \
                	(par['omega_xi'] + small_omega_var_div[var.op.name]**2))))

        # After each task is complete, call update_big_omega and reset_small_omega
        self.update_big_omega = tf.group(*update_big_omega_ops)

        # Reset_small_omega also makes a backup of the final weights, used as hook in the auxiliary loss
        self.reset_small_omega = tf.group(*reset_small_omega_ops)

        # This is called every batch
        self.delta_grads = adam_optimizer.return_delta_grads()
        self.gradients = optimizer_task.compute_gradients(self.pol_loss)

        # Update the samll omegas using the gradients
        for (grad, var) in self.gradients:
            if par['training_method'] == 'RL':
                delta_reward = self.current_reward - self.previous_reward
                update_small_omega_ops.append(tf.assign_add(small_omega_var[var.op.name], self.delta_grads[var.op.name]*delta_reward))
                update_small_omega_ops.append(tf.assign_add(small_omega_var_div[var.op.name], tf.abs(self.delta_grads[var.op.name]*delta_reward)))
            elif par['training_method'] == 'SL':
                update_small_omega_ops.append(tf.assign_add(small_omega_var[var.op.name], -self.delta_grads[var.op.name]*grad ))
                update_small_omega_ops.append(tf.assign_add(small_omega_var_div[var.op.name], self.delta_grads[var.op.name]))

        # Make update group
        self.update_small_omega = tf.group(*update_small_omega_ops) # 1) update small_omega after each train!


    def EWC(self):
        """ Synaptic stabilization via the Kirkpatrick method """

        # Set up method
        var_list = [var for var in tf.trainable_variables() if not 'val' in var.op.name]
        epsilon = 1e-6
        fisher_ops = []
        opt = tf.train.GradientDescentOptimizer(learning_rate = 1.0)

        # Sample from logits
        if par['training_method'] == 'RL':
            log_p_theta = tf.stack([mask*time_mask*action*tf.log(epsilon + pol_out) for (pol_out, action, mask, time_mask) in \
                zip(self.pol_out, self.action, self.mask, self.time_mask)], axis = 0)
        elif par['training_method'] == 'SL':
            log_p_theta = tf.stack([mask*time_mask*tf.log(epsilon + output) for (output, mask, time_mask) in \
                zip(self.output, self.mask, self.time_mask)], axis = 0)

        # Compute gradients and add to aggregate
        grads_and_vars = opt.compute_gradients(log_p_theta, var_list = var_list)
        for grad, var in grads_and_vars:
            print(var.op.name)
            fisher_ops.append(tf.assign_add(self.big_omega_var[var.op.name], \
                grad*grad/par['EWC_fisher_num_batches']))

        # Make update group
        self.update_big_omega = tf.group(*fisher_ops)


def reinforcement_learning(save_fn='test.pkl', gpu_id=None):
    """ Run reinforcement learning training """

    # Isolate requested GPU
    if gpu_id is not None:
        os.environ["CUDA_VISIBLE_DEVICES"] = gpu_id

    # Reset Tensorflow graph before running anything
    tf.reset_default_graph()

    # Set up stimulus and accuracy recording
    accuracy_iter = []
    full_activity_list = []
    agent_records = []
    model_performance = {'reward': [], 'entropy_loss': [], 'val_loss': [], 'pol_loss': [], 'spike_loss': [], 'trial': [], 'task': []}

    # Display relevant parameters
    print_key_info()

    # Start Tensorflow session
    with tf.Session() as sess:

        # Select CPU or GPU
        device = '/cpu:0' if gpu_id is None else '/gpu:0'
        with tf.device(device):
            model = Model()

        # Initialize variables and start the timer
        sess.run(tf.global_variables_initializer())
        t_start = time.time()
        sess.run(model.reset_prev_vars)

        # Begin training loop, iterating over tasks
        task_start_time = time.time()

        for i in range(par['n_train_batches']):

            stimulus_access.place_agents()
            stimulus_access.place_rewards()

            # Calculate and apply gradients
            if par['stabilization'] == 'pathint':
                _, _, _, pol_loss, val_loss, aux_loss, spike_loss, ent_loss, pred_err, stim_pred_err, \
                    rew_pred_err, act_pred_err, h_list, reward_list, pred_loss, expected_reward, actual_reward, agent_locations, action = \
                    sess.run([model.train_op, model.update_current_reward, model.update_small_omega, model.pol_loss, model.val_loss, \
                    model.aux_loss, model.spike_loss, model.entropy_loss, model.total_pred_error, model.stim_pred_error, model.rew_pred_error, model.act_pred_error, \
                    model.h, model.reward, model.pred_loss, model.expected_reward_vector, model.actual_reward_vector, \
                    model.agent_locs, model.action])
                if i>0:
                    sess.run([model.update_small_omega])
                sess.run([model.update_previous_reward])
            elif par['stabilization'] == 'EWC':
                _, _, pol_loss,val_loss, aux_loss, spike_loss, ent_loss, pred_err, stim_pred_err, rew_pred_err, act_pred_err, \
                    h_list, reward_list, agent_locations, action = \
                    sess.run([model.train_op, model.update_current_reward, model.pol_loss, model.val_loss, \
                    model.aux_loss, model.spike_loss, model.entropy_loss, model.total_pred_error, model.stim_pred_error, model.rew_pred_error, model.act_pred_error, \
                    model.h, model.reward, model.agent_locs, model.action])

            # Record accuracies
            reward = np.stack(reward_list)
            rew = np.mean(np.sum(reward, axis=0))
            acc = np.mean(np.sum(reward>0, axis=0))
            accuracy_iter.append(acc)
            if i > 5000:
                if np.mean(accuracy_iter[-5000:]) > 0.98 or (i>25000 and np.mean(accuracy_iter[-20:]) > 0.95):
                    print('Accuracy reached threshold')
                    break

            # Display network performance
            if i%200 == 0:

                context = '_iter{}'.format(i)

                if par['save_plots']:
                    fig, ax = plt.subplots(1,3, figsize=[24,8])
                    im0 = ax[0].imshow(expected_reward[:,:,0], aspect='auto', clim=(-np.abs(expected_reward).max(), np.abs(expected_reward).max()))
                    ax[0].set_title('Expected Reward')
                    im1 = ax[1].imshow(actual_reward[:,:,0], aspect='auto', clim=(-2,2))
                    ax[1].set_title('Actual Reward')
                    diff = expected_reward[:,:,0] - actual_reward[:,:,0]
                    im2 = ax[2].imshow(diff, aspect='auto', clim=(-np.abs(diff).max(), np.abs(diff).max()))
                    ax[2].set_title('Expected - Actual')
                    fig.colorbar(im0, ax=ax[0], orientation='horizontal', ticks=[-np.abs(expected_reward).max(),0,np.abs(expected_reward).max()])
                    fig.colorbar(im1, ax=ax[1], orientation='horizontal', ticks=[-2,0,2])
                    fig.colorbar(im2, ax=ax[2], orientation='horizontal', ticks=[-np.abs(diff).max(),0,np.abs(diff).max()])

                    fn = par['plot_dir'] + par['save_fn'] + '_rewards' + context +par['save_fn_suffix'] + '.png'
                    plt.savefig(fn)
                    plt.clf()
                    plt.close()

                pe  = str([float('{:7.5f}'.format(np.mean(pred_err[i]))) for i in range(len(pred_err))]).ljust(19)
                spe = str([float('{:7.5f}'.format(np.mean(stim_pred_err[i]))) for i in range(len(stim_pred_err))]).ljust(19)
                rpe = str([float('{:7.5f}'.format(np.mean(rew_pred_err[i]))) for i in range(len(rew_pred_err))]).ljust(19)
                ape = str([float('{:7.5f}'.format(np.mean(act_pred_err[i]))) for i in range(len(act_pred_err))]).ljust(19)

                print('Iter: {:>7} | Task: {} | Accuracy: {:5.3f} | Reward: {:5.3f} | Aux Loss: {:7.5f} | Mean h: {:8.5f}'.format(\
                    i, par['task'], acc, rew, aux_loss, np.mean(np.stack(h_list))))
                print('Time: {:>7} | Total PE: {} | Stim PE: {} | Rew PE: {} | Act PE: {}\n'.format(int(np.around(time.time() - task_start_time)), pe, spe, rpe, ape))

                fn = par['save_dir'] + par['save_fn'] + '_trajectories' + par['save_fn_suffix'] + '.pkl'
                agent_records.append({'iter':i, 'reward_locs':stimulus_access.reward_locations,'agent_locs':stimulus_access.loc_history, 'actions':action})
                pickle.dump(agent_records, open(fn.format(i), 'wb'))


        """# Update big omegaes, and reset other values before starting new task
        if par['stabilization'] == 'pathint':
            big_omegas = sess.run([model.update_big_omega, model.big_omega_var])


        elif par['stabilization'] == 'EWC':
            for n in range(par['EWC_fisher_num_batches']):
                name, input_data, _, mk, reward_data = stim.generate_trial(task)
                mk = mk[..., np.newaxis]
                big_omegas = sess.run([model.update_big_omega,model.big_omega_var], feed_dict = \
                    {x:input_data, target: reward_data, gating:par['gating'][task], mask:mk})"""

        #results = {'reward_matrix': reward_matrix, 'par': par, 'activity': full_activity_list}
        #pickle.dump(results, open(par['save_dir'] + save_fn, 'wb') )
        #print('Analysis results saved in', save_fn)
        #print('')

        # Reset the Adam Optimizer, and set the previous parameter values to their current values
        sess.run(model.reset_adam_op)
        sess.run(model.reset_prev_vars)
        if par['stabilization'] == 'pathint':
            sess.run(model.reset_small_omega)

    print('\nModel execution complete. (Reinforcement)')


def print_key_info():
    """ Display requested information """

    key_info = ['synapse_config','spike_cost','weight_cost','entropy_cost','omega_c','omega_xi',\
        'n_hidden','noise_rnn_sd','learning_rate','discount_rate', 'stabilization',\
        'gating_type', 'gate_pct','include_rule_signal','task','num_nav_tuned','room_width','room_height',\
        'rewards','failure_penalty']
    print('\nKey info:')
    print('-'*60)
    for k in key_info:
        print(k.ljust(30), par[k])
    print('-'*60)


def print_reinforcement_results(iter_num, model_performance):
    """ Aggregate and display reinforcement learning results """

    reward = np.mean(np.stack(model_performance['reward'])[-par['iters_between_outputs']:])
    pol_loss = np.mean(np.stack(model_performance['pol_loss'])[-par['iters_between_outputs']:])
    val_loss = np.mean(np.stack(model_performance['val_loss'])[-par['iters_between_outputs']:])
    entropy_loss = np.mean(np.stack(model_performance['entropy_loss'])[-par['iters_between_outputs']:])

    print('Iter. {:4d}'.format(iter_num) + ' | Reward {:0.4f}'.format(reward) +
      ' | Pol loss {:0.4f}'.format(pol_loss) + ' | Val loss {:0.4f}'.format(val_loss) +
      ' | Entropy loss {:0.4f}'.format(entropy_loss))


def get_perf(target, output, mask):
    """ Calculate task accuracy by comparing the actual network output
    to the desired output only examine time points when test stimulus is
    on in another words, when target[:,:,-1] is not 0 """

    output = np.stack(output, axis=0)
    mk = mask*np.reshape(target[:,:,-1] == 0, (par['num_time_steps'], par['batch_size']))

    target = np.argmax(target, axis = 2)
    output = np.argmax(output, axis = 2)

    return np.sum(np.float32(target == output)*np.squeeze(mk))/np.sum(mk)


def append_model_performance(model_performance, reward, entropy_loss, pol_loss, val_loss, trial_num):

    reward = np.mean(np.sum(reward,axis = 0))/par['trials_per_sequence']
    model_performance['reward'].append(reward)
    model_performance['entropy_loss'].append(entropy_loss)
    model_performance['pol_loss'].append(pol_loss)
    model_performance['val_loss'].append(val_loss)
    model_performance['trial'].append(trial_num)

    return model_performance


def generate_placeholders():

    mask = tf.placeholder(tf.float32, shape=[par['num_time_steps'], par['batch_size'], 1])
    x = tf.placeholder(tf.float32, shape=[par['num_time_steps'], par['batch_size'], par['n_input']])  # input data
    target = tf.placeholder(tf.float32, shape=[par['num_time_steps'], par['batch_size'], par['n_pol']])  # input data
    pred_val = tf.placeholder(tf.float32, shape=[par['num_time_steps'], par['batch_size'], par['n_val'], ])
    actual_action = tf.placeholder(tf.float32, shape=[par['num_time_steps'], par['batch_size'], par['n_pol']])
    advantage  = tf.placeholder(tf.float32, shape=[par['num_time_steps'], par['batch_size'], par['n_val']])

    return x, target, mask, pred_val, actual_action, advantage, mask


def main(save_fn='testing', gpu_id=None):

    # Update all dependencies in parameters
    update_dependencies()

    # Identify learning method and run accordingly
    if par['training_method'] == 'SL':
        raise Exception('This code does not support supervised learning at this time.')
    elif par['training_method'] == 'RL':
        reinforcement_learning(save_fn, gpu_id)
    else:
        raise Exception('Select a valid learning method.')


if __name__ == '__main__':
    try:
        if len(sys.argv) > 1:
            main('testing.pkl', sys.argv[1])
        else:
            main('testing.pkl')
    except KeyboardInterrupt:
        print('Quit by KeyboardInterrupt.')
