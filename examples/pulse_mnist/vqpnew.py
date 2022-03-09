# -*- coding: utf-8 -*-
"""VQPnew.ipynb

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/1CZ7HGiMZzaPL29ROUpdn7B35UHysQ01f
"""

#pip install qiskit

import math
import qiskit
from qiskit import pulse, QuantumCircuit
from qiskit.pulse import library
from qiskit.test.mock import FakeQuito
from qiskit.pulse import transforms
from qiskit.pulse.transforms import block_to_schedule
from qiskit.pulse import filters
from qiskit.pulse.filters import composite_filter, filter_instructions
from typing import List, Tuple, Iterable, Union, Dict, Callable, Set, Optional, Any
from qiskit.pulse.instructions import Instruction
from qiskit.compiler import assemble, schedule
import numpy as np
import torch.nn.functional as F
import torch
import time
from torchquantum.datasets import MNIST
from scipy.optimize import minimize
from sklearn.gaussian_process import GaussianProcessRegressor as GPR
from scipy.optimize import LinearConstraint
from sklearn.gaussian_process.kernels import RBF, ConstantKernel, Matern, WhiteKernel
from scipy.stats import norm
import pdb
from qiskit.compiler import assemble, schedule

backend = FakeQuito()

with pulse.build(backend) as pulse_prog:
        qc = QuantumCircuit(4)
        qc.cx(1, 0)
        qc.rz(-4.1026, 3)
        qc.cx(0,1)
        qc.h(3)
        qc.rx(1.2803, 0)
        qc.ry(0.39487, 1)
        qc.crx(-3.025, 0, 2)
        qc.sx(2)
        qc.cx(3,1)
        qc.measure_all()
        print(qc)
        pulse.call(qc)
        print(pulse)
        sched = pulse.Schedule()
        
pulse_prog.draw()

"""Extract amps"""

def is_parametric_pulse(t0, *inst: Union['Schedule', Instruction]):
    inst = t0[1]
    t0 = t0[0]
    if isinstance(inst, pulse.Play) and isinstance(inst.pulse, pulse.ParametricPulse):
        return True
    return False

amps = [play.pulse.amp for _, play in pulse_prog.blocks[0].operands[0].filter(is_parametric_pulse).instructions]
print(amps)

for _, play in pulse_prog.blocks[0].operands[0].filter(is_parametric_pulse).instructions:
    # print(play.pulse.amp)
    pass
    
    
instructions = pulse_prog.blocks[0].operands[0].filter(is_parametric_pulse).instructions

amp_list = list(map(lambda x: x[1].pulse.amp, pulse_prog.blocks[0].operands[0].filter(is_parametric_pulse).instructions))
amp_list = np.array([amp_list])
ampr_list = amp_list.real
ampi_list = amp_list.imag
amp_list = np.append(ampr_list, ampi_list)
rag = np.arange(1,1.1,0.05)
amps_list = [amp_list*x for x in rag]
amps_list = np.array(amps_list)


def get_expectations_from_counts(counts, qubits):
    exps = []
    if isinstance(counts, dict):
        counts = [counts]
    for count in counts:
        ctr_one = [0] * qubits
        total_shots = 0
        for k, v in count.items():
            k = "{0:04b}".format(int(k, 16))
            for qubit in range(qubits):
                if k[qubit] == '1':
                    ctr_one[qubit] += v
            total_shots += v
        prob_one = np.array(ctr_one) / total_shots
        exp = np.flip(-1 * prob_one + 1 * (1 - prob_one))
        exps.append(exp)
    res = np.stack(exps)
    return res

"""BO"""

def acquisition(x_scaled, hyper_param, model, min_Y):  # x_scaled: 1 * dim
    x_scaled = x_scaled.reshape(1, -1)
    if 'LCB' in hyper_param[0]:
        mean, std = model.predict(x_scaled, return_std=True)
        return mean[0] - hyper_param[1] * std[0]
    elif hyper_param[0] == 'EI':
        tau = min_Y
        mean, std = model.predict(x_scaled, return_std=True)
        tau_scaled = (tau - mean) / std
        res = (tau - mean) * norm.cdf(tau_scaled) + std * norm.pdf(tau_scaled)
        return -res  # maximize Ei = minimize -EI
    elif hyper_param[0] == 'PI':
        tau = min_Y
        mean, std = model.predict(x_scaled, return_std=True)
        tau_scaled = (tau - mean) / std
        res = norm.cdf(tau_scaled)
        return -res
    else:
        raise ValueError("acquisition function is not implemented")

def bayes_opt(func, dim_design, N_sim, N_initial, w_bound, hyper_param, store=False, verbose=True, file_suffix=''):
    '''

    :param func: [functional handle], represents the objective function. objective = func(design)
    :param dim_design: [int], the dimension of the design variable
    :param N_sim: [int], The total number of allowable simulations
    :param N_initial: [int], The number of simulations used to set up the initial dataset
    :param w_bound: [(dim_design, 2) np.array], the i-th row contains the lower bound and upper bound for the i-th variable
    :param hyper_param: the parameter for the acquisition function e.g., ['LCB','0.3'], ['EI'], ['PI']
    :param verbose: [Bool], if it is true, print detailed information in each iteration of Bayesian optimization
    :param file_suffix: [string], file suffix used in storing optimization information
    :return:
    cur_best_w: [(dim_design,) np.array], the best design variable
    cur_best_y: [float], the minimum objective value
    '''

    # initialization: set up the training dataset X, Y.
    print("Begin initializing...")
    X = amps_list
    print(X)
    Y = np.zeros((N_initial,))

    # todo: 因为BO没法直接输出complex number，我们需要把实数和虚数部分分成两个部分来训练，再组合成一整个X放回simulator里。
    for i in range(N_initial):
        
        Y[i] = func(X[i, :])
        print("Simulate the %d-th sample... with metric: %.3e" % (i, Y[i])) if verbose else None
    print("Finish initialization with best metric: %.3e" % (np.min(Y)))

    # define several working variables, will be used to store results
    pred_mean = np.zeros(N_sim - N_initial)
    pred_std = np.zeros(N_sim - N_initial)
    acq_list = np.zeros(N_sim - N_initial)

    # Goes into real Bayesian Optimization
    cur_count, cur_best_w, cur_best_y = N_initial, None, 1e10
    while cur_count < N_sim:

        # build gaussian process on the normalized data
        wrk_mean, wrk_std = X.mean(axis=0), X.std(axis=0)
        model = GPR(kernel=ConstantKernel(1, (1e-9, 1e9)) * RBF(1.0, (1e-5, 1e5)), normalize_y=True,
                    n_restarts_optimizer=100)
        model.fit(np.divide(X - wrk_mean, wrk_std), Y)

        # define acquisition function, np.min(Y) is needed in EI and PI, but not LCB
        acq_func = lambda x_scaled: acquisition(x_scaled, hyper_param, model, np.min(Y))

        # optimize the acquisition function independently for N_inner times, select the best one
        N_inner, cur_min, opt = 20, np.inf, None
        for i in range(N_inner):
            w_init = (w_bound[:, 1] - w_bound[:, 0]) * np.random.rand(dim_design) + (
                w_bound[:, 0])
            LC = LinearConstraint(np.eye(dim_design), np.divide(w_bound[:, 0] - wrk_mean, wrk_std),
                                  np.divide(w_bound[:, 1] - wrk_mean, wrk_std), keep_feasible=False)
            cur_opt = minimize(acq_func, np.divide(w_init - wrk_mean, wrk_std), method='COBYLA', constraints=LC,
                               options={'disp': False})
            wrk = acq_func(cur_opt.x)
            if cur_min >= wrk:
                cur_min = wrk
                opt = cur_opt

        # do a clipping to avoid violation of constraints (just in case), and also undo the normalization
        newX = np.clip(opt.x * wrk_std + wrk_mean, w_bound[:, 0], w_bound[:, 1])
        star_time = time.time()
        cur_count += 1
        newY = func(newX)
        end_time = time.time()
        X, Y = np.concatenate((X, newX.reshape(1, -1)), axis=0), np.concatenate((Y, [newY]), axis=0)

        # save and display information
        ind = np.argmin(Y)
        cur_predmean, cur_predstd = model.predict((np.divide(newX - wrk_mean, wrk_std)).reshape(1, -1), return_std=True)
        cur_acq = acq_func(np.divide(newX - wrk_mean, wrk_std))
        cur_best_w, cur_best_y = X[ind, :], Y[ind]
        pred_mean[cur_count - N_initial - 1], pred_std[cur_count - N_initial - 1] = cur_predmean, cur_predstd
        acq_list[cur_count - N_initial - 1] = cur_acq
        if store:
            np.save('./result/X_' + file_suffix + '.npy', X)
            np.save('./result/Y_' + file_suffix + '.npy', Y)
            np.save('./result/cur_best_w_' + file_suffix + '.npy', cur_best_w)
            np.save('./result/cur_best_y_' + file_suffix + '.npy', cur_best_y)
            np.save('./result/pred_mean_' + file_suffix + '.npy', pred_mean)
            np.save('./result/pred_std_' + file_suffix + '.npy', pred_std)
            np.save('./result/acq_list_' + file_suffix + '.npy', acq_list)
        if verbose:
            print("-" * 10)
            print("Number of function evaluations: %d" % cur_count)
            print("Optimize acq message: ", opt.message)
            print("Model predict(new sampled X)... mean: %.3e, std:%.3e" % (cur_predmean, cur_predstd))
            print("Acq(new sampled X): %.3e" % cur_acq)
            print("Y(new sampled X): %.3e, simulation time: %.3e" % (newY, end_time - star_time))
            print("Current best design: ", cur_best_w)
            print("Current best function value: %.3e" % cur_best_y)

    return cur_best_w, cur_best_y
def model(pulse_prog, pulse_encoding):
    quito_sim = qiskit.providers.aer.PulseSimulator.from_backend(FakeQuito())
    for i in range(0, len(pulse_encoding)):
        pulse_sim = assemble(pulse_prog + pulse_encoding[i], backend=quito_sim, shots=512, meas_level = 2, meas_return = 'single')
        results = quito_sim.run(pulse_sim).result()
        counts = results.data()['counts']
        result = get_expectations_from_counts(counts, 4)
        result = torch.tensor(result)
        result = F.log_softmax(result, dim=1)
    
def Fucsimulate(cur_best_w):
    modified_list = cur_best_w[75:]*1j + cur_best_w[:75]
    modified_list = np.ndarray.tolist(modified_list)
    backend = FakeQuito()
    target_all = []
    output_all = []
    dataset = MNIST(
        root='./mnist_data',
        train_valid_split_ratio=[0.9, 0.1],
        digits_of_interest=[3, 6],
    )

    dataflow = dict()

    for split in dataset:
        sampler = torch.utils.data.SequentialSampler(dataset[split])
        dataflow[split] = torch.utils.data.DataLoader(
            dataset[split],
            sampler=sampler,
            num_workers=8,
            pin_memory=True)
    
    device = torch.device("cuda" if use_cuda else "cpu")
    with pulse.build(backend) as pulse_prog:
            qc = QuantumCircuit(4)
            qc.cx(1, 0)
            qc.rz(-4.1026, 3)
            qc.cx(0,1)
            qc.h(3)
            qc.rx(1.2803, 0)
            qc.ry(0.39487, 1)
            qc.crx(-3.025, 0, 2)
            qc.sx(2)
            qc.cx(3,1)
            qc.measure_all()
            print(qc)
            pulse.call(qc)
            print(pulse)
    for inst, amp in zip(pulse_prog.blocks[0].operands[0].filter(is_parametric_pulse).instructions, modified_list):
        inst[1].pulse._amp = amp
        quito_sim = qiskit.providers.aer.PulseSimulator.from_backend(FakeQuito())
    pulse_encoding = [<qiskit.circuit.quantumcircuit.QuantumCircuit object at 0x7fb9008ae0d0>, <qiskit.circuit.quantumcircuit.QuantumCircuit object at 0x7fb9008ae490>, <qiskit.circuit.quantumcircuit.QuantumCircuit object at 0x7fb9008ae9d0>, <qiskit.circuit.quantumcircuit.QuantumCircuit object at 0x7fb9008aedf0>, <qiskit.circuit.quantumcircuit.QuantumCircuit object at 0x7fb9008c0250>, <qiskit.circuit.quantumcircuit.QuantumCircuit object at 0x7fb9008c0670>, <qiskit.circuit.quantumcircuit.QuantumCircuit object at 0x7fb9008c0a90>, <qiskit.circuit.quantumcircuit.QuantumCircuit object at 0x7fb9008c0eb0>, <qiskit.circuit.quantumcircuit.QuantumCircuit object at 0x7fb9008cc310>, <qiskit.circuit.quantumcircuit.QuantumCircuit object at 0x7fb9008cc730>]
    for i in range(0, len(pulse_encoding)):
        pulse_sim = assemble(pulse_prog + pulse_encoding[i], backend=quito_sim, shots=512, meas_level = 2, meas_return = 'single')
        results = quito_sim.run(pulse_sim).result()
        counts = results.data()['counts']
        result = get_expectations_from_counts(counts, 4)
        result = torch.tensor(result)
        result = F.log_softmax(result, dim=1)
        output_all.append(result)
    for feed_dict in dataflow['train']:
        targets = feed_dict['digit'].to(device)
        target_all.append(targets)
    target_all = torch.cat(target_all, dim=0)
    output_all = torch.cat(output_all, dim=0)

    _, indices = output_all.topk(1, dim=1)
    masks = indices.eq(target_all.view(-1, 1).expand_as(indices))
    size = target_all.shape[0]
    corrects = masks.sum().item()
    result = corrects / size
    return 1 - result

if __name__ == '__main__':
    pdb.set_trace()
    seed = 0
    np.random.seed(seed)
    # example: minimize x1^2 + x2^2 + x3^2 + ...
    dim_design = 150
    N_total = 200
    N_initial = 1
    bound = np.ones((dim_design, 2)) * np.array([-1, 1])  # -inf < xi < inf

    func = Fucsimulate
    cur_best_w, cur_best_y = bayes_opt(func, dim_design, N_total, N_initial, bound, ['LCB', 0.3],
                                       store=False, verbose=True, file_suffix=str(seed))

    print(cur_best_w)
    print(cur_best_y)