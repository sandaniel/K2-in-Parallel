from __future__ import division
import numpy as np
import itertools
import pandas as pd
import math
import operator
import time
from mpi4py import MPI

import jodys_serial_v2 as serialv
import parallel_mpi_v4 as v4
import parallel_mpi_v3 as v3
import parallel_mpi_v2 as v2
import parallel_mpi_v1 as v1

def vals_of_attributes(D,n):
    output = []
    for i in xrange(n):
        output.append(list(np.unique(D[:, i])))
    return output

def alpha(df, mask):
    _df = df
    for combo in mask:
        _df = _df[_df[combo[0]] == combo[1]]  # I know there must be a way to speed this up - but i couldn't find it
    return len(_df)

def f(i,pi,attribute_values,df):

    len_pi = len(pi)

    phi_i_ = [attribute_values[item] for item in pi]
    if len(phi_i_) == 1:
        phi_i = [[item] for item in phi_i_[0]]
    else:
        phi_i = list(itertools.product(*phi_i_))

    # bug fix: phi_i might contain empty tuple (), which shouldn't be counted in q_I
    try:
        phi_i.remove(())
    except ValueError:
        pass

    q_i = len(phi_i)

    V_i = attribute_values[i]
    r_i = len(V_i)

    #product = 1
    product = 0
    #numerator = math.factorial(r_i - 1)
    numerator = np.sum([np.log(b) for b in range(1, r_i)])

    # special case: q_i = 0
    if q_i == 0:
        js = ['special']
    else:
        js = range(q_i) 

    for j  in js:

        # initializing mask to send to alpha
        if j == 'special':
            mask = []
        else:
            mask = zip(pi,phi_i[j])

        # initializing counts that will increase with alphas
        N_ij = 0
        #inner_product = 1
        inner_product = 0

        for k in xrange(r_i):
            # adjusting mask for each k
            mask_with_k = mask + [[i,V_i[k]]]
            alpha_ijk = alpha(df,mask_with_k)
            N_ij += alpha_ijk
            #inner_product = inner_product*math.factorial(alpha_ijk)
            inner_product = inner_product + np.sum([np.log(b) for b in range(1, alpha_ijk+1)])
        #denominator = math.factorial(N_ij + r_i - 1)
        denominator = np.sum([np.log(b) for b in range(1, N_ij+r_i)])
        #product = product*(numerator/denominator)*inner_product
        product = product + numerator - denominator + inner_product
    return product

def find_all_jobs(i,rank,size):
    p1 = i[np.floor(i/size) % 2 == 0]
    p1 =  p1[p1%size == rank]
    p2 = i[np.floor(i/size) % 2 == 1]
    p2 =  p2[size - 1 - p2%size == rank]
    return sorted(list(p1) + list(p2), reverse = True)

def parent_set(i,node_order,attribute_values,df,u=2):
        OKToProceed = False
        pi = []
        pred = node_order[0:i]
        P_old = f(node_order[i],pi,attribute_values,df)
        if len(pred) > 0:
            OKToProceed = True
        while (OKToProceed == True and len(pi) < u):
            iters = [item for item in pred if item not in pi]
            if len(iters) > 0:
                f_to_max = {};
                for z_hat in iters:
                    f_to_max[z_hat] = f(node_order[i],pi+[z_hat],attribute_values,df)
                z = max(f_to_max.iteritems(), key=operator.itemgetter(1))[0]
                P_new = f_to_max[z]
                if P_new > P_old:
                    P_old = P_new
                    pi = pi+[z]
                else:
                    OKToProceed = False
            else:
                OKToProceed = False
        return pi

def k2_in_parallel(D,node_order,comm,rank,size,u=2):
    status = MPI.Status()
    n = D.shape[1]
    assert len(node_order) == n, "Node order is not correct length.  It should have length %r" % n
    m = D.shape[0]
    attribute_values = vals_of_attributes(D,n)

    # we'll need this constant later for message sizes
    lsig = int(np.floor(n/(2*size)))

    df = pd.DataFrame(D)
    OKToProceed = False
    parents = {}

    #selecting_job_time = 0
    #calculation_time = 0

    all_i = find_all_jobs(np.arange(n),rank,size)

    friends = range(rank + 1, size) + range(rank)
    friend_in_need = np.array([-1], dtype=np.int32)

    # this is the signal we'll send to friends who ask for work to let them know that we don't have any for them
    done = np.zeros(lsig, dtype = np.int32)
    done[0] = -2

    firstchunk = all_i[0:int(3/4*len(all_i))]
    secondchunk = all_i[int(3/4*len(all_i)):len(all_i)]

    for i in firstchunk:
        parents[node_order[i]] = parent_set(i, node_order, attribute_values, df, u)

    lsec = len(secondchunk)

    while lsec > 0:

        req = comm.Irecv(friend_in_need, source = MPI.ANY_SOURCE) # this needs to be non-blocking, asynchronous communication -- no pickle option available
        if req.Test(status = status) == False:
            req.Cancel()

        # in this case we do work
        if not friend_in_need == -1:
            friend = friend_in_need

            # this friend asked for work, so don't send work to this friend later
            friends.remove(friend)

            # send done message if we don't have a lot of work left
            if lsec < 4:
                comm.Send(done, dest = friend)

            # send half of the remaining work if there is enough left,
            else:
                # build the message
                a = list(secondchunk[np.ceil(1/2*lsec):lsec])
                # pad the message with zeros (for consistent-sized messages)
                b = list(np.zeros(lsig-len(a)))
                # send the message
                comm.Send(np.array(a+b, dtype=np.int32), dest=friend)
                # update my own chunk of work
                secondchunk = secondchunk[0:np.ceil(1/2*lsec)]

                i = secondchunk.pop(0) 
                parents[node_order[i]] = parent_set(i, node_order, attribute_values, df, u)

            friend_in_need =  np.array([-1], dtype=np.int32)


        # whether you sent to a friend or not choose the next element to calculate
        i = secondchunk.pop(0) 
        parents[node_order[i]] = parent_set(i, node_order, attribute_values, df, u)

        # update lsec to see if we should go again
        lsec = len(secondchunk)

    # nodes that are done with work ask their neighbors for work units
    # you could receive up to np.floor(n/(2*size)) work units
    signal = np.zeros(shape = lsig, dtype = np.int32)

    while(len(friends) > 0):
        destination = friends.pop(0)
        mess = np.array([rank], dtype=np.int32)
        sreq = comm.Isend(mess, dest = destination) #sending rank as message eliminates need for Get_source()
        comm.Recv(signal, source = destination) # this should be blocking - can't do anything without it
        
        # get any work from message that exists
        all_i = signal[signal > 0]
        for i in all_i:
            parents[node_order[i]] = parent_set(i, node_order, attribute_values, df, u)

    # sending parents back to node 0 for sorting and printing
    p = comm.gather(parents, root = 0)

    if rank == 0:
    # gather returns a list - converting to a single dictionary
        parents = {}

        for i in range(len(p)):
            parents.update(p[i])

        print parents
        return parents
    


if __name__ == "__main__":
    comm = MPI.COMM_WORLD
    rank = comm.Get_rank()
    size = comm.Get_size()
    #device = pycuda.autoinit.device.pci_bus_id()
    #node = MPI.Get_processor_name()
    
    if rank == 0:
        D = np.random.binomial(1,0.9,size=(100,40))
        node_order = list(range(40))
    else:
        D = None
        node_order = None

    D = comm.bcast(D, root=0)
    node_order = comm.bcast(node_order, root = 0)

    comm.barrier()
    start = MPI.Wtime()
    k2_in_parallel(D,node_order,comm,rank,size,u=10)
    comm.barrier()
    end = MPI.Wtime()
    if rank == 0:
        print "V5 Parallel Computing Time: ", end-start