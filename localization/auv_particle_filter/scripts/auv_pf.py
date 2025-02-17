#!/usr/bin/python

# Standard dependencies
import sys
import os
import math
import rospy
import numpy as np
import tf
import tf2_ros
from scipy.special import logsumexp # For log weights

from geometry_msgs.msg import Pose, PoseArray, PoseWithCovarianceStamped
from geometry_msgs.msg import Transform, Quaternion, TransformStamped
from nav_msgs.msg import Odometry
from tf.transformations import quaternion_from_euler, euler_from_quaternion
from tf.transformations import translation_matrix, translation_from_matrix
from tf.transformations import quaternion_matrix, quaternion_from_matrix
from tf.transformations import rotation_matrix, rotation_from_matrix

# For sim mbes action client
from sensor_msgs.msg import PointCloud2
import sensor_msgs.point_cloud2 as pc2

# Import Particle() class
from auv_particle import Particle, matrix_from_tf, pcloud2ranges

# Multiprocessing and parallelizing
import numba
from numba import jit
from resampling import residual_resample, naive_resample, systematic_resample, stratified_resample 

# import time # For evaluating mp improvements
# import multiprocessing as mp
# from functools import partial # Might be useful with mp
# from pathos.multiprocessing import ProcessingPool as Pool

class auv_pf(object):

    def __init__(self):
        # Read necessary parameters
        self.pc = rospy.get_param('~particle_count', 10) # Particle Count
        map_frame = rospy.get_param('~map_frame', 'map') # map frame_id
        odom_frame = rospy.get_param('~odom_frame', 'odom') 
        meas_model_as = rospy.get_param('~mbes_as', '/mbes_sim_server') # map frame_id
        mbes_pc_top = rospy.get_param("~particle_sim_mbes_topic", '/sim_mbes')

        # Initialize tf listener
        tfBuffer = tf2_ros.Buffer()
        tf2_ros.TransformListener(tfBuffer)
        try:
            rospy.loginfo("Waiting for transforms")
            mbes_tf = tfBuffer.lookup_transform('hugin/base_link', 'hugin/mbes_link',
                                                rospy.Time(0), rospy.Duration(10))
            self.base2mbes_mat = matrix_from_tf(mbes_tf)
            
            m2o_tf = tfBuffer.lookup_transform(map_frame, odom_frame,
                                               rospy.Time(0), rospy.Duration(10))
            self.m2o_mat = matrix_from_tf(m2o_tf)

            rospy.loginfo("Transforms locked - pf node")
        except:
            rospy.loginfo("ERROR: Could not lookup transform from base_link to mbes_link")

        # Read covariance values
        meas_cov = float(rospy.get_param('~measurement_covariance', 0.01))
        cov_string = rospy.get_param('~motion_covariance')
        cov_string = cov_string.replace('[','')
        cov_string = cov_string.replace(']','')
        cov_list = list(cov_string.split(", "))
        motion_cov = list(map(float, cov_list))

        cov_string = rospy.get_param('~init_covariance')
        cov_string = cov_string.replace('[','')
        cov_string = cov_string.replace(']','')
        cov_list = list(cov_string.split(", "))
        init_cov = list(map(float, cov_list))


        # Initialize list of particles
        self.particles = np.empty(self.pc, dtype=object)

        for i in range(self.pc):
            self.particles[i] = Particle(i, self.pc, self.base2mbes_mat, self.m2o_mat, init_cov=init_cov, meas_cov=meas_cov,
                                     process_cov=motion_cov, map_frame=map_frame, odom_frame=odom_frame,
                                     meas_as=meas_model_as, pc_mbes_top=mbes_pc_top)

        self.time = None
        self.old_time = None
        self.pred_odom = None
        self.latest_mbes = PointCloud2()
        self.prev_mbes = PointCloud2()
        self.poses = PoseArray()
        self.poses.header.frame_id = odom_frame 
        self.avg_pose = PoseWithCovarianceStamped()
        self.avg_pose.header.frame_id = odom_frame

        # Initialize particle poses publisher
        pose_array_top = rospy.get_param("~particle_poses_topic", '/particle_poses')
        self.pf_pub = rospy.Publisher(pose_array_top, PoseArray, queue_size=10)

        # Initialize average of poses publisher
        avg_pose_top = rospy.get_param("~average_pose_topic", '/average_pose')
        self.avg_pub = rospy.Publisher(avg_pose_top, PoseWithCovarianceStamped, queue_size=10)

        # Establish subscription to mbes pings message
        mbes_pings_top = rospy.get_param("~mbes_pings_topic", 'mbes_pings')
        rospy.Subscriber(mbes_pings_top, PointCloud2, self.mbes_callback)

        # Establish subscription to odometry message (intentionally last)
        odom_top = rospy.get_param("~odometry_topic", 'odom')
        rospy.Subscriber(odom_top, Odometry, self.odom_callback)

        # Expected meas of PF outcome at every time step
        pf_mbes_top = rospy.get_param("~average_mbes_topic", '/avg_mbes')
        self.pf_mbes_pub = rospy.Publisher(pf_mbes_top, PointCloud2, queue_size=1)

        rospy.loginfo("Particle filter class successfully created")

        self.update_rviz()
        rospy.spin()

    def mbes_callback(self, msg):
        self.latest_mbes = msg

    def odom_callback(self, odom_msg):
        self.time = odom_msg.header.stamp.to_sec()
        if self.old_time and self.time > self.old_time:
            # Motion prediction
            self.predict(odom_msg)
            
            if self.latest_mbes.header.stamp > self.prev_mbes.header.stamp:    
                # Measurement update if new one received
                weights = self.update(self.latest_mbes, odom_msg)
                self.prev_mbes = self.latest_mbes
                
                # Particle resampling
                self.resample(weights)

            self.update_rviz()
        self.old_time = self.time

    def predict(self, odom_t):
        #  dt = 0.1
        dt = self.time - self.old_time
        for i in range(0, self.pc):
            self.particles[i].motion_pred(odom_t, dt)
         
    def update(self, meas_mbes, odom):
        # Compute AUV MBES ping ranges
        particle_tf = Transform()
        particle_tf.translation = odom.pose.pose.position
        particle_tf.rotation    = odom.pose.pose.orientation
        tf_mat = matrix_from_tf(particle_tf)
        m2auv = np.matmul(self.m2o_mat, np.matmul(tf_mat, self.base2mbes_mat))
        mbes_meas_ranges = pcloud2ranges(meas_mbes, m2auv)

        # Measurement update of each particle
        weights = []
        for i in range(self.pc):
            self.particles[i].meas_update(mbes_meas_ranges)
            weights.append(self.particles[i].w)

        weights_array = np.asarray(weights)
        # Add small non-zero value to avoid hitting zero
        weights_array += 1.e-30

        return weights_array


    def resample(self, weights):

        print "-------------"
        # Normalize weights
        weights /= weights.sum()
        #  print "Weights"
        #  print weights
        
        N_eff = self.pc
        if weights.sum() == 0.:
            rospy.loginfo("All weights zero!")
        else:
            N_eff = 1/np.sum(np.square(weights))

        print "N_eff ", N_eff 
        # Resampling?
        if N_eff < self.pc*0.5:
            indices = residual_resample(weights)
            print "Indices"
            print indices
            keep = list(set(indices))
            lost = [i for i in range(self.pc) if i not in keep]
            dupes = indices[:].tolist()
            for i in keep:
                dupes.remove(i)
            
            self.reassign_poses(lost, dupes)
            
            # Add noise to particles
            for i in range(self.pc):
                self.particles[i].add_noise([3.,3.,0.,0.,0.,0.0])

        else:
            print N_eff
            rospy.loginfo('Number of effective particles high - not resampling')

    def reassign_poses(self, lost, dupes):
        for i in range(len(lost)):
            # Faster to do separately than using deepcopy()
            self.particles[lost[i]].p_pose.position.x = self.particles[dupes[i]].p_pose.position.x
            self.particles[lost[i]].p_pose.position.y = self.particles[dupes[i]].p_pose.position.y
            self.particles[lost[i]].p_pose.position.z = self.particles[dupes[i]].p_pose.position.z
            self.particles[lost[i]].p_pose.orientation.x = self.particles[dupes[i]].p_pose.orientation.x
            self.particles[lost[i]].p_pose.orientation.y = self.particles[dupes[i]].p_pose.orientation.y
            self.particles[lost[i]].p_pose.orientation.z = self.particles[dupes[i]].p_pose.orientation.z
            self.particles[lost[i]].p_pose.orientation.w = self.particles[dupes[i]].p_pose.orientation.w

    def average_pose(self, pose_list):
        
        poses_array = np.array(pose_list)
        ave_pose = poses_array.mean(axis = 0)
        
        self.avg_pose.pose.pose.position.x = ave_pose[0]
        self.avg_pose.pose.pose.position.y = ave_pose[1]
        self.avg_pose.pose.pose.position.z = ave_pose[2]
        roll  = ave_pose[3]
        pitch = ave_pose[4]
        """
        Average of yaw angles creates
        issues when heading towards pi because pi and
        negative pi are next to eachother, but average
        out to zero (opposite direction of heading)
        """
        yaws = poses_array[:,5]
        if np.abs(yaws).min() > math.pi/2:
            yaws[yaws < 0] += 2*math.pi
        yaw = yaws.mean()

        self.avg_pose.pose.pose.orientation = Quaternion(*quaternion_from_euler(roll, pitch, yaw))
        self.avg_pose.header.stamp = rospy.Time.now()
        self.avg_pub.publish(self.avg_pose)

        # Hacky way to get the expected MBES ping from avg pose of PF
        # TODO: do this properly :d
        pf_ping = self.particles[0].predict_meas(self.avg_pose.pose.pose)
        self.pf_mbes_pub.publish(pf_ping)


    # TODO: publish markers instead of poses
    #       Optimize this function
    def update_rviz(self):
        self.poses.poses = []
        pose_list = []
        for i in range(self.pc):
            self.poses.poses.append(self.particles[i].p_pose)
            pose_vec = self.particles[i].get_pose_vec()
            pose_list.append(pose_vec)
        # Publish particles with time odometry was received
        self.poses.header.stamp = rospy.Time.now()
        self.pf_pub.publish(self.poses)
        self.average_pose(pose_list)


if __name__ == '__main__':

    rospy.init_node('auv_pf')
    try:
        auv_pf()
    except rospy.ROSInterruptException:
        rospy.logerr("Couldn't launch pf")
        pass
