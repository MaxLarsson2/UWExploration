#include "auv_model/auv_motion_simple.hpp"


int main(int argc, char** argv){

    ros::init(argc, argv, "auv_motion_model");
    ros::NodeHandle nh("~");

    // Inputs
    std::string track_str, map_str, output_str;
    double rate_odom, rate_meas;
    nh.param<double>("odom_rate", rate_odom, 1);
    nh.param<double>("meas_rate", rate_meas, 1);

    AUVMotionModel* auv_mm = new AUVMotionModel(ros::this_node::getName(), nh);
    auv_mm->init();
    ros::Timer timer1 = nh.createTimer(ros::Duration(rate_odom), &AUVMotionModel::updateMotion, auv_mm);
    ros::Timer timer2 = nh.createTimer(ros::Duration(rate_meas), &AUVMotionModel::updateMeas, auv_mm);

    ros::spin();
    ros::waitForShutdown();

    if(!ros::ok()){
        delete auv_mm;
    }
    ROS_INFO("auv_motion_model finished");

    return 0;
}
