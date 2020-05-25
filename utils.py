import pandas as pd
import csv
import numpy as np
from numpy.linalg import norm
from numpy import exp,sin,cos,pi
import ahrs
from ahrs import Quaternion


def load_endaq_log(prefix,t_min=0,t_max=3600,g=9.799):

    """
    Read in the cluster of CSV files that are autogenerated from the endaq proprietary analysis program
    The specific fields can be hard-coded since they wont change.
    Make sure the configuration on the device is set to record these channels.
    raw accelerations come out in units of g's, and converted here to m/s^2
    t_filter = callable function to filter data based on time-values
    """

    raw_dat={}
    # These are the file names that come from the endaq automatic data export
    channels=[ \
        "Ch80_8g_DC_Acceleration.csv", \
        "Ch32_16g_DC_Acceleration.csv", \
        "Ch43_IMU_Acceleration.csv", \
        "Ch47_Rotation.csv", \
        "Ch51_IMU_Magnetic_Field.csv" ]

    # These keys are chosed here for convenience
    raw_keys=[\
     "acc8", \
     "acc16", \
     "accIMU", \
     "gyro", \
     "mag"]

    # Loop over all channel,key pairs
    for (ch,key) in zip(channels,raw_keys):
        try:
            #Load as an iterator to avoid putting too much into memory at once
            iter_csv=pd.read_csv(prefix+ch,chunksize=1000,names=["t","x","y","z"],\
                iterator=True,header=None)

            #Use a list comprehension to join all the chunks
            raw_dat[key]=pd.concat([ chunk[ \
                            (chunk["t"] >= t_min) & (chunk["t"] <= t_max  )   ]  \
                            for chunk in iter_csv  \
                            ])

            # Deprecated method loading all samples at once
            #raw_dat[key]=pd.read_csv(prefix+ch,header=None,names=["t","x","y","z"])

            ## Make the time column into the index, using the timedelta data type
            raw_dat[key].index =pd.to_timedelta(raw_dat[key]['t'],unit="s")
            del raw_dat[key]['t']

            print("Successfully loaded ",ch)
        except:
            print("Failed attempting to load ",ch)

    ## Convert from units of g's to m/s^2 using a measurement of known local gravity
    for acc in ['acc8','acc16','accIMU']:
        if acc in raw_dat.keys():
            raw_dat[acc] = raw_dat[acc].apply(lambda q: q*g if q.name in ['x', 'y','z'] else q)

    return raw_dat

def subangle(v1,v2):
    """
    Compute the angle subtended between two vectors
    """
    return np.arccos(np.dot(v1,v2)/(norm(v1)*norm(v2)))


def R(lie_vec):
    """
    Compute the 3x3 rotation matrix from the lie generator
    """
    # Return the identity matrix if lie_vec=0
    if not np.any(lie_vec): return np.identity(3)
    ### Otherwise, compute axis-angle params

    #This factor of 2 comes from the SO(3) SU(2) double cover
    angle=norm(lie_vec)/2
    n=lie_vec/norm(lie_vec)

    ###Construct the rotation matrix using the axis and angle
    c,s = cos(angle), sin(angle)
    x,y,z=n[0],n[1],n[2]
    return np.array([\
     [ c+x**2*(1-c),   x*y*(1-c)-z*s, x*z*(1-c)+y*s],\
     [ y*x*(1-c)+z*s,  c+y**2*(1-c),  y*z*(1-c)-x*s],\
     [ z*x*(1-c)-y*s,  z*y*(1-c)+x*s, c+z**2*(1-c) ]\
     ])

def axb2(a,b,sumall=True):
    """
    Vectorized wrapper for computing: (Sum) |axb|^2
    row index is the first index, as with drawing matrices on paper
    a,b = 3xN arrays
    Symmetry: f(a,b)=f(b,a)
    """
    if sumall:
        return np.sum(np.cross(a,b,axisa=0,axisb=0,axisc=0)**2)
    return np.sum(np.cross(a,b,axisa=0,axisb=0,axisc=0)**2,axis=0)



def alignment_cost(a,b):
    """
    Given a series of observed data, construct a const function which only has R as an arg
    """
    return lambda lie_params: axb2(a,R(lie_params).dot(b))


def lie_angle(lie_vec,unit="rad"):
    if unit=="rad":
        return norm(lie_vec)/2
    if unit=="deg":
        return (180/pi)*norm(lie_vec)/2


def cal_matrix(params):
    """
    params=[scale1,scale2,scale3 lie_vec1,lie_vec2,lie_vec3]
    """
    return R(params[3:6]).dot(np.diag(params[0:3]))

def calibration_cost(a,b):
    """
    |a-XRb|^2
    for a scale matrix X, rotation matrix R.
    params=[scale1,scale2,scale3 lie_vec1,lie_vec2,lie_vec3,offset1,offset2,offset3]
    """

    return lambda params: np.sum(( a- cal_matrix(params).dot(b)   )**2)



def synchronize_series(series,ref=None):
    """
    Resample data according to timestamps in a reference time series
    """

    # Input validation
    if type(series) is not pd.DataFrame and type(series) is not pd.Series:
        raise TypeError("Input: series must be a pandas dataframe or time series")
    if type(ref) is not pd.DataFrame and type(ref) is not pd.Series:
        raise TypeError("Input: series must be a pandas dataframe or time series")

    ## Interpolate series at the time points in ref.index, and then loc only ref.index
    return series.reindex(\
        series.index.union( ref.index     )\
        ).interpolate().loc[ref.index]


def idx_filter(t,data,intervals):
    """
    Filter the data inside the intervals
    params:
        t=[t1,t2,..,tn]
        data=[data1,data2,...,datan]
        intervals=[(a1,b1),(a2,b2),...]
    output:
        data[t in (a1,b1) or t in (a2,b2) ,...]
    """
    mask=np.array([False]*len(t))
    for interval in intervals:
        mask|= (interval[0]<t) & (t<interval[1])
    return t[mask],data[mask,:]



def apply_ahrs(gyro,acc,mag,ts,\
               q0=np.array([1.0,0.0,0.0,0.0]) ,g=np.array([0,0,9.799]),\
               position=False,zero_period=0.300,workspace=dict(),   \
               filter="IMU", betaval = 0.1, reset = True\
               ):
    """
    Measured value of local gravity is 9.799 m/s^2
    """
    ## input validation
    assert len(gyro)==len(acc)
    assert len(acc)==len(mag)
    assert len(mag)==len(ts)

    try:
        q0=np.array(q0/np.linalg.norm(q0),dtype=np.float)
    except ValueError:
        raise ValueError("input q0 could not be coerced into an array of np.float")

    try:
        g=np.array(g,dtype=np.float)
    except ValueError:
        raise ValueError("input g could not be coerced into an array of np.float")

    assert len(q0)==4,"Input q0 must be length 4 to be considered a quaternion"
    assert len(g) ==3,"Input g must be length 3 to be a valid gravity vector"


    ## Compute the frequency and number of samples
    dt=np.mean(ts[1:]-ts[0:-1])
    freq=1/dt
    num_samples=len(ts)

    # Initialize the AHRS filter
    madgwick=ahrs.filters.Madgwick(beta=betaval,frequency=freq)

    # Allocate arrays using kwarg q0 as the initial reference orientation
    if 'Q' not in workspace.keys():
       workspace['Q']=  np.tile(q0,(num_samples,1))
    workspace['Q'][0]=q0

    if 'Q_quat' not in workspace.keys():
       workspace['Q_quat']=  np.tile(Quaternion(q0),(num_samples,1))
    workspace['Q_quat'][0]=Quaternion(q0)

    if 'acc_lab' not in workspace.keys():
       workspace['acc_lab']=np.zeros((num_samples,3))

    QIMU_quat=np.tile(Quaternion(q0),(num_samples,1))

    ## position bool controls whether the position,velocity estimator runs
    if position:

        if 'state' not in workspace.keys():
            workspace['state']=np.zeros((num_samples,6))
        ## Process transition matrix
        ## x=x0 + v0 dt + 1/2 (a-g) dt^2
        A=np.matrix(\
        [\
        [1,  0,  0,  dt,  0,  0  ] ,\
        [0,  1,  0,  0,  dt,  0  ] ,\
        [0,  0,  1,  0,  0,  dt  ] ,\
        [0,  0,  0,  1,  0,  0   ] ,\
        [0,  0,  0,  0,  1,  0   ] ,\
        [0,  0,  0,  0,  0,  1   ] ,\
        ]\
        )
        ## Control matrix
        B=np.matrix(\
        [\
        [dt**2/2,  0,    0    ] ,\
        [0,    dt**2/2,  0    ] ,\
        [0,        0,  dt**2/2] ,\
        [dt,       0,    0    ] ,\
        [0,       dt,    0    ] ,\
        [0,        0,    dt   ] ,\
        ]\
        )

        ## set the interval between zeroing the state
        next_zero=zero_period

    # For each time step apply the estimation filter
    for t in range(1,num_samples):

        # Orientation estimation using madwick filter
        # Default gains are tuned for IMU not MARG
        if filter == "IMU":
          workspace['Q'][t]=madgwick.updateIMU(workspace['Q'][t-1],gyro[t],acc[t])
        elif filter == "MARG":
          workspace['Q'][t]=madgwick.updateMARG(workspace['Q'][t-1],gyro[t],acc[t],mag[t])

        workspace['Q_quat'][t]=Quaternion(workspace['Q'][t])
        #Rotate the acceleration vector from sensor frame to lab frame
        workspace['acc_lab'][t]=Quaternion(workspace['Q'][t]).rotate(acc[t])-g

        if position:
           #Update the state using the process and control matrices
           workspace['state'][t]=A.dot(workspace['state'][t-1])+B.dot(workspace['acc_lab'][t-1])

           ## Periodically re-zero the state
           ## This is for inspecting the short-term performance
           if ts[t] > next_zero:
               workspace['state'][t]=np.zeros(6)
               next_zero=ts[t]+zero_period
    if position:
        return workspace['acc_lab'],workspace['Q'],workspace['state']
    return workspace['acc_lab'],workspace['Q']



def msqError(params,intervals,acc,gyro,mag,ts,g=np.array([0,0,9.799]),workspace=dict()):
    """
    Evaluate the msq error of the calibration/orientation model
    params=[scalex, scaley,scalez, biasx,biasy,biasz, q_w,q_i,q_j,q_k]
    intervals=[(a1,b1),(a2,b2),...]
        sections of the calibration data set which are quiet
        used for calculating the msq error at the end
    acc:
        acclerometer time series
    gyro:
        gyroscope time series
    mag:
        magnetometer time series
    ts:
        time values
    g:
        known gravity vector
    workspace:
        dictionary of arrays that is preallocated
    make sure that acc,gyro,mag are time synced
    """
    scale=params[0:3]
    bias=params[3:6]
    q0=params[6:]

    ## Apply scale-bias calibration
    workspace['acc_scratch']=calibrate(acc,params)

    #Apply the madgwick filter to get the heading correction
    #Using the initial orientation
    workspace['acc_scratch'],_=apply_ahrs(gyro,workspace['acc_scratch'],mag,ts,q0=q0,g=g,workspace=workspace)

    # Use compare the measured acceleration in the lab frame to the gravitational model
    # When everything is calibrated, the lab frame acceleration should be zero during the time period
    # specified in intervals
    _,workspace['acc_scratch']=idx_filter(ts,workspace['acc_scratch'],intervals)
    return 3*np.mean(workspace['acc_scratch']**2)


## Generated 2020-05-12
## meansq error: 0.0016
recent_cal=[ 1.00019457,  1.00488652,  0.98081785, \
             0.00157861,  0.03279509,  0.19761574, \
             0.99666972,  0.00134338,  0.0370082 , -0.02798724]
def calibrate(data,params=recent_cal):
    """
    Apply a scale-bias operation on data
    data= num_samples x 3 np.array()
    params=[scale0,scale1,scale2,bias0,bias1,bias2]
    """
    if params is None :
        return data
    scale=np.array(params[0:3],dtype=np.float)
    bias= np.array(params[3:6],dtype=np.float)
    return data.dot(np.diag(scale))+bias

#def kalman_filter(t=np.array([0]),acc_lab=np.array(len(t)*[[0,0,0]]),z=len(t)*[[0,0,0,0,0,0]],q=6*[0.01],r=6*[0.01]):
def kalman_filter(t=[0],acc_lab=[0,0,0],z=[[0,0,0,0,0,0]],q=[0.1,0.1,0.1,0.1,0.1,0.1],r=[0.1,0.1,0.1,0.1,0.1,0.1],reset=False,time_period=0.300,acc_var=[-0.0063, -0.0501, 0.0099]):
    
    t=np.asarray(t)
    acc_lab=np.asarray(acc_lab)
    
    dt=np.mean(t[1:]-t[0:-1])

    #Kalman corrected state vector, accuracy matrix, and Kalman gain as arrays
    #Velocity will be used everywhere as is from the state vector since we aren't measuring it

    state_guess = len(t)*[[0,0,0,0,0,0]]
    P=len(t)*[np.matrix(np.diag([0,0,0,0,0,0]))] 
    K=[np.matrix(np.diag([0,0,0,0,0,0]))]
    next_zero=-1 #For intermittent resetting
    #Fake GPS data]


    #F is old state to new state Matrix, B is acceleration to new state Matrix

    F=np.matrix(\
    [\
    [1,  0,  0,  dt,  0,  0  ] ,\
    [0,  1,  0,  0,  dt,  0  ] ,\
    [0,  0,  1,  0,  0,  dt  ] ,\
    [0,  0,  0,  1,  0,  0   ] ,\
    [0,  0,  0,  0,  1,  0   ] ,\
    [0,  0,  0,  0,  0,  1   ] ,\
    ]\
    )
    B=np.matrix(\
    [\
    [dt**2/2,  0,    0    ] ,\
    [0,    dt**2/2,  0    ] ,\
    [0,        0,  dt**2/2] ,\
    [dt,       0,    0    ] ,\
    [0,       dt,    0    ] ,\
    [0,        0,    dt   ] ,\
    ]\
    )

    #Q covariance of sensor axes matrix, R Covariance of GPS uncertainty Matrix

    #Acceleration Variance Vector
    
    #Noise in state equation is B times noise in acceleration
    #Use sum of variance relation to obtain
    Q,R = np.matrix(np.diag(q)),np.matrix(np.diag(r))
    position_variances = [0,0,0,0,0,0]
    
    #for i in range(6):
    #    position_variances[i]=B.item((i,0))*acc_var[0]+B.item((i,1))*acc_var[1]+B.item((i,2))*acc_var[2]+q[i]
    #Q=np.matrix(np.diag(position_variances))

    #Initialise variables 

    for i in range(len(t)):
        z[i]=[z[i][0],z[i][1],z[i][2],0,0,0]
    #    state_guess[i]=[state_guess[i][0],state_guess[i][1],state_guess[i][2],state[i][3],state[i][4],state[i][5]]

    #create naive state guess and naive accuracy matrix
    state_naive,P_naive = state_guess[0],P[0]
    #Kalman filter loop
    for i in range(1,len(t)):
        #Predict Equations
        state_naive=np.asarray((F.dot(state_guess[i-1])+B.dot(acc_lab[i])))[0]
        P_naive = F.dot(P[i-1])
        P_naive = P_naive.dot(F.transpose())+Q
        if i>1:
            for j in [3,4,5]:
                z[i][j]=state_guess[i-1][j]
        #Update Equations
        K=P_naive.dot(np.linalg.inv(P_naive+R))
        y=np.asarray(z[i])-state_naive
        state_guess[i]=state_naive+np.asarray(K.dot(y))[0]
        P[i]=(np.matrix(np.identity(6))-K).dot(P_naive)
        #for i in range(6):
        #    position_variances[i]+=B.item((i,0))*acc_var[0]+B.item((i,1))*acc_var[1]+B.item((i,2))*acc_var[2]
        #Q=np.matrix(np.diag(position_variances))
        if reset:
            if t[i]>next_zero:
                state_guess[i]=np.asarray([0,0,0,0,0,0])
                P[i]=np.matrix(np.diag([0,0,0,0,0,0]))
                next_zero+=time_period
                #Q=np.matrix(np.diag(q))
                #position_variances=q
                #print(Q)
        
    return state_guess

    

    