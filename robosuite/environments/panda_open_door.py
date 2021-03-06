from collections import OrderedDict
import numpy as np
import copy

from robosuite.utils.transform_utils import convert_quat
from robosuite.environments.panda import PandaEnv
from gym.envs.robotics.rotations import quat2euler, euler2quat, mat2euler, quat_mul, quat_conjugate

from robosuite.utils import transform_utils as T

from robosuite.models.arenas import TableCabinetArena
from robosuite.models.objects import FullyFrictionalBoxObject, CylinderObject
from robosuite.models.robots import Panda
from robosuite.models.tasks import TableTopTask, UniformRandomSamplerObjectSpecific

from robosuite.class_wrappers import change_dof

# https://stackoverflow.com/a/13849249/11815215

def unit_vector(vector):
    """ Returns the unit vector of the vector.  """
    return vector / np.linalg.norm(vector)

def angle_between(v1, v2):
    """ Returns the angle in radians between vectors 'v1' and 'v2'::

            >>> angle_between((1, 0, 0), (0, 1, 0))
            1.5707963267948966
            >>> angle_between((1, 0, 0), (1, 0, 0))
            0.0
            >>> angle_between((1, 0, 0), (-1, 0, 0))
            3.141592653589793
    """
    v1_u = unit_vector(v1)
    v2_u = unit_vector(v2)
    return np.arccos(np.clip(np.dot(v1_u, v2_u), -1.0, 1.0))

def sin_cos_encoding(arr):
    """ Encode an array of angle value to correspongding Sines and Cosines, avoiding value jump in 2PI measure like from PI to -PI. """
    return np.concatenate((np.sin(arr), np.cos(arr)))

class PandaOpenDoor(change_dof(PandaEnv, 8, 8)): # keep the dimension to control the gripper; better not remove change_dof
    """
    This class corresponds to the pushing task for the Panda robot arm.
    """
    
    parameters_spec = {
        **PandaEnv.parameters_spec,
        'table_size_0': [0.7, 0.9],
        'table_size_1': [0.7, 0.9],
        'table_size_2': [0.7, 0.9],
        #'table_friction_0': [0.4, 1.6],
        'table_friction_1': [0.0025, 0.0075],
        'table_friction_2': [0.00005, 0.00015],
        # 'boxobject_size_0': [0.018, 0.022],
        # 'boxobject_size_1': [0.018, 0.022],
        # 'boxobject_size_2': [0.018, 0.022],
        # 'boxobject_friction_0': [0.04, 1.6],
        #'boxobject_friction_1': [0.0025, 0.0075],    # fixed this to zero
        # 'boxobject_friction_2': [0.00005, 0.00015],
        # 'boxobject_density_1000': [0.6, 1.4],
    }
    
    def reset_props(self,
                    table_size_0=0.8, table_size_1=0.8, table_size_2=0.9,
                    table_friction_0=0., table_friction_1=0.005, table_friction_2=0.0001,
                    # boxobject_size_0=0.020, boxobject_size_1=0.020, boxobject_size_2=0.020,
                    # boxobject_friction_0=0.1, boxobject_friction_1=0.0, boxobject_friction_2=0.0001,
                    # boxobject_density_1000=0.1,
                    **kwargs):
        
        self.table_full_size = (table_size_0, table_size_1, table_size_2)
        self.table_friction = (table_friction_0, table_friction_1, table_friction_2)
        # self.boxobject_size = (boxobject_size_0, boxobject_size_1, boxobject_size_2)
        # self.boxobject_friction = (boxobject_friction_0, boxobject_friction_1, boxobject_friction_2)
        # self.boxobject_density = boxobject_density_1000 * 1000.
        super().reset_props(**kwargs)

    def __init__(self,
                 use_object_obs=True,
                 reward_shaping=True,
                 placement_initializer=None,
                 object_obs_process=True,
                 **kwargs):
        """
        Args:

            use_object_obs (bool): if True, include object (cube) information in
                the observation.

            reward_shaping (bool): if True, use dense rewards.

            placement_initializer (ObjectPositionSampler instance): if provided, will
                be used to place objects on every reset, else a UniformRandomSampler
                is used by default.

            object_obs_process (bool): if True, process the object observation to get a task_state.
                Setting this to False is useful when some transformation (eg. noise) need to be done to object observation raw data prior to the processing.
        """
        
        # whether to use ground-truth object states
        self.use_object_obs = use_object_obs

        # reward configuration
        self.reward_shaping = reward_shaping

        # object placement initializer
        if placement_initializer:
            self.placement_initializer = placement_initializer
        else:
            # self.placement_initializer = UniformRandomSampler(
            #     x_range=[-0.1, 0.1],
            #     y_range=[-0.1, 0.1],
            #     ensure_object_boundary_in_range=False,
            #     z_rotation=None,
            # )
            self.placement_initializer = UniformRandomSamplerObjectSpecific(
                x_ranges=[[-0.03, -0.02], [0.09, 0.1]],
                y_ranges=[[-0.05, -0.04], [-0.05, -0.04]],
                ensure_object_boundary_in_range=False,
                z_rotation=None,
            )
            

        # for first initialization
        self.table_full_size = (0.8, 0.8, 0.8)
        self.table_friction = (0., 0.005, 0.0001)
        # self.boxobject_size = (0.02, 0.02, 0.02)
        # self.boxobject_friction = (0.1, 0.005, 0.0001)
        # self.boxobject_density = 100.

        self.object_obs_process = object_obs_process

        super().__init__(gripper_visualization=True, **kwargs)

    def _load_model(self):
        """
        Loads an xml model, puts it in self.model
        """
        super()._load_model()
        self.mujoco_robot.set_base_xpos([0, 0, 0])

        # load model for table top workspace
        self.mujoco_arena = TableCabinetArena(
            table_full_size=self.table_full_size, table_friction=self.table_friction
        )
        if self.use_indicator_object:
            self.mujoco_arena.add_pos_indicator()

        # The panda robot has a pedestal, we want to align it with the table
        self.mujoco_arena.set_origin([0.16 + self.table_full_size[0] / 2, 0, 0])
        
        # initialize objects of interest

        # cube = FullyFrictionalBoxObject(
        #     size=self.boxobject_size,
        #     friction=self.boxobject_friction,
        #     density=self.boxobject_density,
        #     rgba=[1, 0, 0, 1],
        # )
        # self.mujoco_cube = cube
        # goal = CylinderObject(
        #     size=[0.03, 0.001],
        #     rgba=[0, 1, 0, 1],
        # )
        # self.mujoco_goal = goal
        # self.mujoco_objects = OrderedDict([("cube", cube), ("goal", goal)])
        
        self.mujoco_objects = None

        # task includes arena, robot, and objects of interest
        self.model = TableTopTask(
            self.mujoco_arena,
            self.mujoco_robot,
            self.mujoco_objects,
            initializer=self.placement_initializer,
            visual_objects=[],
        )
        if self.mujoco_objects is not None:
            self.model.place_objects()

    def _get_reference(self):
        """
        Sets up references to important components. A reference is typically an
        index or a list of indices that point to the corresponding elements
        in a flatten array, which is how MuJoCo stores physical simulation data.
        """
        super()._get_reference()
        # self.cube_body_id = self.sim.model.body_name2id("cube")
        self.l_finger_geom_ids = [
            self.sim.model.geom_name2id(x) for x in self.gripper.left_finger_geoms
        ]
        self.r_finger_geom_ids = [
            self.sim.model.geom_name2id(x) for x in self.gripper.right_finger_geoms
        ]
        # self.cube_geom_id = self.sim.model.geom_name2id("cube")

        # gripper ids
        # self.goal_body_id = self.sim.model.body_name2id('goal')
        # self.goal_site_id = self.sim.model.site_name2id('goal')

    def _reset_internal(self):
        """
        Resets simulation internal configurations.
        """
        super()._reset_internal()
        self.sim.forward()

        # reset positions of objects
        if self.mujoco_objects is not None:
            self.model.place_objects()

        # reset joint positions
        # init_pos = self.mujoco_robot.init_qpos
        # print(init_pos)
        # init_pos += np.random.randn(init_pos.shape[0]) * 0.02
        # self.sim.data.qpos[self._ref_joint_pos_indexes] = np.array(init_pos)

        # self.sim.data.qpos[self._ref_joint_pos_indexes] = [0.02085236,  0.20386552,  0.00569112, -2.60645364,  2.8973697, 3.53509316, 2.89737955]  # a initial gesture: facing downwards
        # self.sim.data.qpos[self._ref_joint_pos_indexes] = [ 0.10234113, -1.32314358,  0.18383693, -2.88485115,  1.64673455,  3.22235274, 2.3644487 ] # a good initial gesture： facing horizontally
        self.sim.data.qpos[self._ref_joint_pos_indexes] = [ 0.10259647, -0.77839656,  0.27246156, -2.35741103,  1.647504,  3.43102572, -0.85707793]
        # open the gripper
        # self.sim.data.qpos[self._ref_joint_gripper_actuator_indexes] = np.array([0.2, -0.2])
        self.sim.data.ctrl[-2:] = np.array([0.04, -0.04])  # panda gripper finger joint range is -0.04~0.04

        # set other reference attributes
        eef_rot_in_world = self.sim.data.get_body_xmat("right_hand").reshape((3, 3))
        self.world_rot_in_eef = copy.deepcopy(eef_rot_in_world.T)  # TODO inspect on this: should we set a golden reference other than a initial position?

    # reward function from sawyer_push
    def reward(self, action=None):
        """
        Reward function for the task.

        The dense reward has three components.

            Reaching: in [-inf, 0], to encourage the arm to reach the object
            Goal Distance: in [-inf, 0] the distance between the pushed object and the goal
            Safety reward in [-inf, 0], -1 for every joint that is at its limit.

        The sparse reward only receives a {0,1} upon reaching the goal

        Args:
            action (np array): The action taken in that timestep

        Returns:
            reward (float): the reward
            previously in robosuite-extra, when dense reward is used, the return value will be a dictionary. but we removed that feature.
        """
        reward = 0.
        self.door_open_angle = abs(self.sim.data.get_joint_qpos("hinge0"))

        reward_door_open = self.door_open_angle

        reward_dist = 0.
        reward_ori = 0.
        if self.door_open_angle < 0.03:
            # A distance reward: minimize the distance between the gripper and door konb when the door is almost closed 
            reward_dist = -np.linalg.norm(self.get_finger2knob_dist_vec())

            # An orientation reward: make the orientation of gripper horizontal (better for knob grasping) when the door is almost closed 
            fingerEulerDesired =  [np.pi, 0, np.pi/2]  # horizontal gesture for gripper
            finger_ori = self.get_finger_ori()

            ori_diff = sin_cos_encoding(fingerEulerDesired) - sin_cos_encoding(finger_ori)  # use sin_cos_encoding to avoid value jump in 2PI measure
            reward_ori = -np.linalg.norm(ori_diff) * 0.04

            # worldHknob = self.sim.data.get_body_xquat("knob_link")
            # knobHee_desired = euler2quat(quat2euler([0.5, 0.5, 0.5, -0.5]))
            # worldHee_desired = quat_mul(worldHknob, knobHee_desired)

            # print(quat2euler(knobHee_desired), self.get_finger_ori())

        reward = reward_door_open + reward_dist + reward_ori  # A summary of reward values


        # Success Reward
        success = self._check_success()
        if (success):
            reward += 0.1
            self.done = True



        # worldHknob = self.sim.data.get_body_xquat("knob_link")
        # knobHee_desired = euler2quat(quat2euler([0.5, 0.5, -0.5, 0.5]))
        # worldHee_desired = quat_mul(worldHknob, knobHee_desired)
        # print(quat2euler(worldHee_desired), quat2euler(worldHknob), self.get_finger_ori() )


        # # sparse completion reward
        # if not self.reward_shaping and self._check_success():
        #     reward = 1.0

        # # use a dense reward
        # if self.reward_shaping:
        #     object_pos = self.sim.data.body_xpos[self.cube_body_id]

        #     # max joint angles reward
        #     joint_limits = self._joint_ranges
        #     current_joint_pos = self._joint_positions

        #     hitting_limits_reward = - int(any([(x < joint_limits[i, 0] + 0.05 or x > joint_limits[i, 1] - 0.05) for i, x in
        #                                       enumerate(current_joint_pos)]))

        #     reward += hitting_limits_reward

        #     # reaching reward
        #     gripper_site_pos = self.sim.data.site_xpos[self.eef_site_id]
        #     dist = np.linalg.norm(gripper_site_pos - object_pos)
        #     reaching_reward = -0.4 * dist
        #     reward += reaching_reward

        #     # print(gripper_site_pos, object_pos, reaching_reward)

        #     # Success Reward
        #     success = self._check_success()
        #     if (success):
        #         reward += 0.1

        #     # goal distance reward
        #     goal_pos = self.sim.data.site_xpos5707963267948966bject--gripper--goal
        #     angle_g_o_g = angle_between(gripper_site_pos - object_pos,
        #                                 goal_pos - object_pos)
        #     if not success and angle_g_o_g < np.pi / 2.:
        #         reward += -0.03 - 0.02 * (np.pi / 2. - angle_g_o_g)

        #     # print('grippersitepos', gripper_site_pos,
        #     #       'objpos', object_pos,
        #     #       'jointangles', hitting_limits_reward,
        #     #       'reaching', reaching_reward,
        #     #       'success', success,
        #     #       'goaldist', goal_distance_reward)

        #     unstable = reward < -2.5

        #     # Return all three types of rewards
        #     reward = {"reward": reward, "reaching_distance": -10 * reaching_reward,
        #               "goal_distance": - goal_distance_reward,
        #               "hitting_limits_reward": hitting_limits_reward,
        #               "unstable":unstable}

        return reward
    
    def _check_success(self):
        """
        Returns True if task has been completed.
        """

        if self.door_open_angle >= 1.55: # 1.57 ~ PI/2
            return True
        else:
            return False

        # object_pos = self.sim.data.body_xpos[self.cube_body_id]
        # goal_pos = self.sim.data.site_xpos[self.goal_site_id]

        # dist = np.linalg.norm(goal_pos - object_pos)
        # goal_horizontal_radius = self.model.mujoco_objects['goal'].get_horizontal_radius()

        # # object centre is within the goal radius
        # return dist < goal_horizontal_radius
        # return False

    def step(self, action):
        # """ explicitly shut the gripper """
        # joined_action = np.append(action, [1.])
        return super().step(action)

    def world2eef(self, world):
        return self.world_rot_in_eef.dot(world)

    # def put_raw_object_obs(self, di):
        # Extract position and velocity of the eef
        # eef_pos_in_world = self.sim.data.get_body_xpos("right_hand")
        # eef_xvelp_in_world = self.sim.data.get_body_xvelp("right_hand")

        # print('eef_pos_in_world', eef_pos_in_world)

        # Get the position, velocity, rotation  and rotational velocity of the object in the world frame
        # object_pos_in_world = self.sim.data.body_xpos[self.cube_body_id]
        # object_xvelp_in_world = self.sim.data.get_body_xvelp('cube')
        # object_rot_in_world = self.sim.data.get_body_xmat('cube')
        
        # Get the z-angle with respect to the reference position and do sin-cosine encoding
        # world_rotation_in_reference = np.array([[0., 1., 0., ], [-1., 0., 0., ], [0., 0., 1., ]])
        # object_rotation_in_ref = world_rotation_in_reference.dot(object_rot_in_world)
        # object_euler_in_ref = T.mat2euler(object_rotation_in_ref)
        # z_angle = object_euler_in_ref[2]
        
        # object_quat = convert_quat(self.sim.data.body_xquat[self.cube_body_id], to='xyzw')
        
        # Get the goal position in the world
        # goal_site_pos_in_world = np.array(self.sim.data.site_xpos[self.goal_site_id])

        # Record observations into a dictionary
        # di['goal_pos_in_world'] = goal_site_pos_in_world
        # di['eef_pos_in_world'] = eef_pos_in_world
        # di['eef_vel_in_world'] = eef_xvelp_in_world
        # di['object_pos_in_world'] = object_pos_in_world
        # di['object_vel_in_world'] = object_xvelp_in_world
        # di["z_angle"] = np.array([z_angle])
        # di['object_quat'] = object_quat

    # def process_object_obs(self, di):
    #     # z_angle = di['z_angle']
    #     # sine_cosine = np.array([np.sin(8*z_angle), np.cos(8*z_angle)]).reshape((2,))

    #     eef_to_object_in_world = di['object_pos_in_world'] - di['eef_pos_in_world']
    #     # eef_to_object_in_eef = self.world2eef(eef_to_object_in_world)

    #     object_to_goal_in_world = di['goal_pos_in_world'] - di['object_pos_in_world']
    #     # object_to_goal_in_eef = self.world2eef(object_to_goal_in_world)

    #     # object_xvelp_in_eef = self.world2eef(di['object_vel_in_world'])
    #     # eef_xvelp_in_eef = self.world2eef(di['eef_vel_in_world'])

        # task_state = np.concatenate([
                                    #  eef_to_object_in_world,
                                    #  object_to_goal_in_world,
                                    #  di['eef_pos_in_world'],
                                    #  di['eef_vel_in_world'],
                                    #  di['object_vel_in_world'],
                                    #  di['object_quat']
                                    # ])
        # di['task_state'] = task_state

    def _get_observation(self):
        """
        Returns an OrderedDict containing observations [(name_string, np.array), ...].

        Important keys:
            gripper_to_object : The x-y component of the gripper to object distance
            object_to_goal : The x-y component of the object-to-goal distance
            object_z_rot : the roation of the object around an axis sticking out the table

            object_xvelp: x-y linear velocity of the object
            gripper_xvelp: x-y linear velocity of the gripper


            task-state : a concatenation of all the above.
        """
        # di = super()._get_observation()  # joint angles & vel, which we don't need.
        di = OrderedDict()

        # camera observations
        if self.use_camera_obs:
            camera_obs = self.sim.render(
                camera_name=self.camera_name,
                width=self.camera_width,
                height=self.camera_height,
                depth=self.camera_depth,
            )
            if self.camera_depth:
                di["image"], di["depth"] = camera_obs
            else:
                di["image"] = camera_obs

        # low-level object information
        if self.use_object_obs:
            eef_pos_in_world = self.sim.data.get_body_xpos("right_hand")
            eef_xvelp_in_world = self.sim.data.get_body_xvelp("right_hand")
            di['eef_pos_in_world'] = eef_pos_in_world  # dim=3
            di['eef_vel_in_world'] = eef_xvelp_in_world  # dim=3
            di['finger_knob_dist'] = self.get_finger2knob_dist_vec()  # dim=3
            di['door_hinge_angle'] = [self.sim.data.get_joint_qpos("hinge0")]  # dim=1

            task_state = np.concatenate([
                                    di['eef_pos_in_world'], 
                                    di['eef_vel_in_world'], 
                                    di['finger_knob_dist'],
                                    di['door_hinge_angle'],
                                ])
            di['task_state'] = task_state
        return di

    def _check_contact(self):
        """
        Returns True if gripper is in contact with an object.
        """
        collision = False
        for contact in self.sim.data.contact[: self.sim.data.ncon]:
            if (
                self.sim.model.geom_id2name(contact.geom1)
                in self.gripper.contact_geoms()
                or self.sim.model.geom_id2name(contact.geom2)
                in self.gripper.contact_geoms()
            ):
                collision = True
                break
        return collision

    def _check_contact_with(self, object):
        """
        Returns True if gripper is in contact with an object.
        """
        collision = False
        for contact in self.sim.data.contact[: self.sim.data.ncon]:
            if (
                    (self.sim.model.geom_id2name(contact.geom1) in self.gripper.contact_geoms()
                     and contact.geom2 == self.sim.model.geom_name2id(object))

                    or (self.sim.model.geom_id2name(contact.geom2) in self.gripper.contact_geoms()
                        and contact.geom1 == self.sim.model.geom_name2id(object))
            ):
                collision = True
                break
        return collision

    def _gripper_visualization(self):
        """
        Do any needed visualization here. Overrides superclass implementations.
        """
        if self.gripper_visualization:
            rgba = np.zeros(4)

            self.sim.model.site_rgba[self.eef_site_id] = rgba


    def get_finger_ori(self):
        finger_rel_quat = self.sim.data.get_body_xquat("rightfinger")
        hand_quat = self.sim.data.get_body_xquat("right_hand")
        finger_world_quat = quat_mul(finger_rel_quat, hand_quat)  # TODO: which one at first?
        return quat2euler(finger_world_quat)

    def get_hand_pos(self):
        return self.sim.data.get_geom_xpos("hand_visual")

    def get_knob_pos(self):
        knob_pos = self.sim.data.get_geom_xpos("center_cabinet_knob")
        return knob_pos

    def get_finger2knob_dist_vec(self):
        return self.get_hand_pos() - self.get_knob_pos()
