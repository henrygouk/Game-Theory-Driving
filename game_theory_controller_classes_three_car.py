# -*- coding: utf-8 -*-
from trajectory_type_definitions import evaluate,putCarOnTraj,ReversedTrajectory,StoppedTrajectory
import math
import nashpy as nash
import numbers
import numpy as np
import random
import re

import sys
sys.path.insert(0,'../driving_simulator')
import road_classes

import datetime #only need for writing file for preferences to prevent overwriting or doubling up
import main

PREFERENCE_FILENAME = "Game_Theory_Controller_Preferences"
ROLLOUT_FILENAME = "Game_Theory_Controller_Rollout"


class GameTheoryDrivingController():

    def __init__(self,ego,ego_traj_list,traj_builder,goal_function=None,reactive_car=None,non_reactive_car=None,non_ego_traj_list=None,write=False,debug=False,**kwargs):

        self.initialisation_params = {'ego':ego,'ego_traj_list':ego_traj_list,'traj_builder':traj_builder,\
                'goal_function':goal_function,'reactive_car':reactive_car,'non_reactive_car':non_reactive_car,'non_ego_traj_list':non_ego_traj_list}
        self.initialisation_params.update(kwargs)

        if goal_function is not None:
            #Specified goal used to compute reward
            self.goal_function = goal_function
        else:
            #No motivation
            self.goal_function = zeroFunction

        self.E_global_cost = None
        self.NE_global_cost = None

        self.traj_builder = traj_builder

        self.setup(ego=ego,ego_traj_list=ego_traj_list,reactive_car=reactive_car,non_reactive_car=non_reactive_car,non_ego_traj_list=non_ego_traj_list)

        self.t = 0
        self.ego_traj_index = None
        self.ego_traj = None

        self.reactive_car_time_distr = None
        self.non_reactive_car_time_distr = None

        self.write = write
        self.debug = debug

        if write and "DUMMY" not in ego.label:
            self.ego_preference_file = initialiseFile(PREFERENCE_FILENAME+"_{}".format(self.ego.label),self.ego.label,self.ego.timestep,non_ego_traj_list)
            self.reactive_car_preference_file = initialiseFile(PREFERENCE_FILENAME+"_{}".format(self.reactive_car.label),self.ego.label,self.ego.timestep,non_ego_traj_list)
            self.rollout_file = initialiseFile(ROLLOUT_FILENAME,self.ego.label,self.ego.timestep,None)


    def setup(self,ego=None,ego_traj_list=None,reactive_car=None,non_reactive_car=None,non_ego_traj_list=None):
        if ego is not None:
            self.ego = ego
            self.ego_state = {} #gets updated in end step so neither agent has more information than the other
            self.ego_traj_list = ego_traj_list
            self.built_ego_traj_list = None
            self.ego_preference = [1/len(ego_traj_list) for _ in ego_traj_list]
            self.ego_preference_order = [0 for _ in range(len(self.ego_preference))]
            self.initialisation_params.update({'ego': ego, 'ego_traj_list': ego_traj_list})
        if reactive_car is not None:
            self.reactive_car = reactive_car
            self.reactive_car_state = {} #gets updated in end step so neither agent has more information than the other
            self.reactive_car_traj_list = non_ego_traj_list
            self.built_reactive_car_traj_list = [self.traj_builder.makeTrajectory(x,self.reactive_car.state) for x in self.reactive_car_traj_list]
            self.reactive_car_preference = [1/len(self.reactive_car_traj_list) for _ in self.reactive_car_traj_list]
            self.reactive_car_preference_order = [0 for _ in range(len(self.reactive_car_preference))]
            self.initialisation_params.update({'reactive_car': reactive_car, 'reactive_car_traj_list': self.reactive_car_traj_list})
        if non_reactive_car is not None:
            self.non_reactive_car = non_reactive_car
            self.non_reactive_car_state = {} #gets updated in end step so neither agent has more information than the other
            self.non_reactive_car_traj_list = non_ego_traj_list
            self.built_non_reactive_car_traj_list = [self.traj_builder.makeTrajectory(x,self.non_reactive_car.state) for x in self.non_reactive_car_traj_list]
            self.non_reactive_car_preference = [1/len(self.non_reactive_car_traj_list) for _ in self.non_reactive_car_traj_list]
            self.non_reactive_car_preference_order = [0 for _ in range(len(self.non_reactive_car_preference))]
            self.initialisation_params.update({'non_reactive_car': non_reactive_car, 'non_reactive_car_traj_list': self.non_reactive_car_traj_list})

            self.policy_pairs = {}
            self.plausible_other_trajectory_indices = []


    def selectAction(self,*args):
        if self.debug: print(f"\n Selecting Action for: {self.ego.label}")
        if self.ego_traj is not None:
            if abs(self.ego_traj.velocity(self.t)-self.ego_state["velocity"])>1:
                print("Velocity Error:\nEgo: {}\nShould be: {} per {}\n".format(self.ego_state,self.ego_traj.state(self.t,self.ego.Lr+self.ego.Lf),self.ego_traj.label))
#                exit(-1)

        #We maintain an estimate of both agents preferences in order to approximate a perspective
        #common to both agents

        #print("\nEgo State: {}".format(self.ego.state))
        #print("Non-Ego State: {}\n".format(self.other.state))

        is_cost_matrix_changed = False # we use this as another check if the equilibrium needs to be rechecked

        if self.reactive_car_time_distr is None:
            max_timesteps = int(max([x.traj_len_t/self.reactive_car.timestep for x in self.built_reactive_car_traj_list]))
            #self.other_time_distr = [1/max_timesteps for _ in range(max_timesteps+1)]
            self.reactive_car_time_distr = [.99] + [.01/(max_timesteps-1) for _ in range(max_timesteps)]
            self.reactive_car_time_distr = [x/sum(self.reactive_car_time_distr) for x in self.reactive_car_time_distr]

        if self.non_reactive_car_time_distr is None:
            max_timesteps = int(max([x.traj_len_t/self.non_reactive_car.timestep for x in self.built_non_reactive_car_traj_list]))
            #self.other_time_distr = [1/max_timesteps for _ in range(max_timesteps+1)]
            self.non_reactive_car_time_distr = [.99] + [.01/(max_timesteps-1) for _ in range(max_timesteps)]
            self.non_reactive_car_time_distr = [x/sum(self.non_reactive_car_time_distr) for x in self.non_reactive_car_time_distr]

        t0 = datetime.datetime.now()
        t_cur = t0
        if self.ego_traj is None:
            #print("Ego is resetting trajectories")
            self.built_ego_traj_list = [self.traj_builder.makeTrajectory(x,self.ego.state) for x in self.ego_traj_list]

            #Compute the costs each agent experiences based on the assumption that both agents obey the rules of
            # the road and don't want to crash
            # This reward matrix does not include individual preferences of either agents
            self.E_global_cost,self.NE_global_cost = computeGlobalCostMatrix(self.t,self.ego,self.built_ego_traj_list,self.reactive_car,self.built_reactive_car_traj_list,self.reactive_car_time_distr)
            is_cost_matrix_changed = True

        t1 = datetime.datetime.now()
        if self.debug: print("Making Ego Trajectories and Computing Cost Matrix takes: {}".format((t1-t_cur).microseconds))
        t_cur = t1
        if self.t != 0:
            self.reactive_car_time_distr = updateTimeDistr(self.reactive_car,self.reactive_car_state,self.built_reactive_car_traj_list,self.reactive_car_preference,self.reactive_car_time_distr,self.reactive_car.timestep)
            self.non_reactive_car_time_distr = updateTimeDistr(self.non_reactive_car,self.non_reactive_car_state,self.built_non_reactive_car_traj_list,self.non_reactive_car_preference,self.non_reactive_car_time_distr,self.non_reactive_car.timestep)
            #print("Self time is: {}\tUpdated other time distribution is: {}".format(self.t,self.other_time_distr))
            likely_reactive_car_t = [i for i,x in enumerate(self.reactive_car_time_distr) if x==max(self.reactive_car_time_distr)][0]*self.reactive_car.timestep
            likely_non_reactive_car_t = [i for i,x in enumerate(self.non_reactive_car_time_distr) if x==max(self.non_reactive_car_time_distr)][0]*self.non_reactive_car.timestep
            if likely_reactive_car_t == 0:
                #If most likely time is 0 then we believe other agent must have completed their last trajectory. Therefore we rebuild new trajectories based on the assumption they
                # must choose a new one
                #print(f"Rebuilding Trajectories for {self.other.label}")
                self.built_reactive_car_traj_list = [self.traj_builder.makeTrajectory(x,self.reactive_car.state) for x in self.reactive_car_traj_list]
                self.E_global_cost,self.NE_global_cost = computeGlobalCostMatrix(self.t,self.ego,self.built_ego_traj_list,self.reactive_car,self.built_reactive_car_traj_list,self.reactive_car_time_distr)
                is_cost_matrix_changed = True
            self.ego_preference = updatePreference(self.t,self.ego,self.ego_state,self.built_ego_traj_list,self.ego_preference)
            self.reactive_car_preference = updatePreference(likely_reactive_car_t,self.reactive_car,self.reactive_car_state,self.built_reactive_car_traj_list,self.reactive_car_preference)
            self.non_reactive_car_preference = updatePreference(likely_non_reactive_car_t,self.non_reactive_car,self.non_reactive_car_state,self.built_non_reactive_car_traj_list,self.non_reactive_car_preference)
            if self.debug: print("Updating other preferences takes: {}".format((datetime.datetime.now()-t_cur).microseconds))
            t_cur = datetime.datetime.now()

        if self.debug: print(f"\nPreferences are: \t\t{self.ego_traj_list}\nEgo: \t{self.ego_preference}\nReactive: \t{self.reactive_car_preference}\nNon Reactive: \t{self.non_reactive_car_preference}\n")


        ego_preference_order = [self.ego_preference.index(x) for x in sorted(self.ego_preference)]
        reactive_car_preference_order = [self.reactive_car_preference.index(x) for x in sorted(self.reactive_car_preference)]

        #The nash equilibrium is used to identify what the other person is likely to do. If they don't believe either agent's preferences have changed then the
        # equilibrium will be the same.
        if is_cost_matrix_changed or ego_preference_order != self.ego_preference_order or reactive_car_preference_order != self.reactive_car_preference_order:
            #First step we will estimate the cost matrix that NE estimates in order to determine what they
            #  are motivated to do.

            self.ego_preference_order = list(ego_preference_order)
            self.reactive_car_preference_order = list(reactive_car_preference_order)

            E_cost_estimate = list(self.E_global_cost)
            NE_cost_estimate = list(self.NE_global_cost)

            for i,row in enumerate(E_cost_estimate):
                new_row = []
                for j,entry in enumerate(row):
                    #if entry != -1: entry,_ = computeReward(self.t,self.ego.copy(),self.built_ego_traj_list[i],self.other.copy(),self.built_other_traj_list[j],self.other_time_distr,\
                    #        veh1_reward_function=self.goal_function,veh2_reward_function=main.changeLaneRightRewardFunction)
                    if entry != -1: entry = self.ego_preference[i]
                    #if entry != -1: entry = self.other_preference[j] #ego totally compliant to non-ego
                    new_row.append(entry)
                E_cost_estimate[i] = list(new_row)

            for i,row in enumerate(NE_cost_estimate):
                new_row = []
                for j,entry in enumerate(row):
                    if entry != -1: entry = self.reactive_car_preference[i]
                    #if entry != -1: _, entry = computeReward(self.t,self.ego.copy(),self.built_ego_traj_list[j],self.other.copy(),self.built_other_traj_list[i],self.other_time_distr,\
                    #        veh1_reward_function=self.goal_function,veh2_reward_function=main.changeLaneRightRewardFunction)
                    new_row.append(entry)
                NE_cost_estimate[i] = list(new_row)

            t2 = datetime.datetime.now()
            if self.debug: print("Updating Cost Matrices takes: {}".format((t2-t_cur).microseconds))

            if self.debug:
                print("\nPrinting E's estimated cost matrix")
                for row in E_cost_estimate:
                    print(row)
                print("Ending print of E's estimated cost matrix\n")

                print("\nPrinting NE's estimated cost matrix")
                for row in NE_cost_estimate:
                    print(row)
                print("Ending print of NE's estimated cost matrix\n")

            #These policies give us the optimal policies for E to follow under the assumption that
            # NE is rational and the assumption that the preferences have been correctly estimated
            E_policies,NE_policies = computeNashEquilibria(E_cost_estimate,NE_cost_estimate,self.debug)
            t3 = datetime.datetime.now()
            if self.debug: print("Computing Nash Equilibrium takes: {}".format((t3-t2).microseconds))
            t_cur = t3

            #print(f"\n\nSelecting Action for {self.ego.label}")
            #print("E Cost Estimate:")
            #for row in E_cost_estimate:
            #    print(row)

            #print("\nNE Cost Estimate")
            #for row in NE_cost_estimate:
            #    print(row)
            #print("\n")

            #print("NE believes optimal policies are")
            #for i,(E_pol,NE_pol) in enumerate(zip(E_policies,NE_policies)):
            #    print(f"{i}: {E_pol}\t{NE_pol}")


            #BASED ON THE POLICIES THAT MAXIMISE NE'S EXPECTED PAYOFF E MUST NOW DETERMINE THEIR
            # BEST ACTION BASED ON THEIR KNOWN, TRUE REWARD FUNCTION

            #SOMEHOW INCORPORATE COMPLIANCE INTO EXPECTED VALUE COMPUTATION BY RELIEVING THE ASSUMPTION
            # OF RATIONALITY ON NE. I.E. WE NO LONGER ASSUME THEY CAN ANTICIPATE THAT A CRASH WILL OCCUR
            # PAYOFF IS AMPLIFIED BY COMPLIANCE FOR THE MAXIMAL ESTIMATED PREFERENCE
            # E MUST THEN DETERMINE THEIR OPTIMAL BEHAVIOUR
            # SOMEHOW THIS IS THEN USED TO DETERMINE WHAT E'S ACTION SHOULD BE

            #IF THE EXPECTED PAYOFF TO NE OF BEHAVING IRRATIONALLY IS HIGHER THAN BEHAVING RATIONALLY,
            # BEHAVE IN COMPLIANCE WITH THE OTHER

            #COMPLIANCE SHOULD BE AN ESTIMATE FOR HOW LIKELY NE IS TO BEHAVE AS WE WANT THEM TO
            #THUS WE SHOULD BE ABLE TO SPECIFY A DESIRED TARGET FOR NE, AND THEN BY CHOOSING ACTIONS
            # CONSTRUCT A GAME IN WHICH THEY ARE INCENTIVISED TO GO TO WHERE WE WANT TO, BY MANIPULATING
            # WHAT THEY WOULD WANT TO DO BASED ON THE COMPLIANCE VALUE

            max_ep = None
            NE_best_policies = []
            for a,(E_p,NE_p) in enumerate(zip(E_policies,NE_policies)):
                ep = 0
                for j in range(len(E_p)):
                    for i in range(len(NE_p)):
                        ep += E_p[j]*NE_p[i]*NE_cost_estimate[i][j]
                if max_ep is None or ep>=max_ep:
                    if max_ep is None or ep>max_ep:
                        max_ep = ep
                        NE_best_policies = []
                    if a not in NE_best_policies:
                        NE_best_policies.append(a)

            self.policy_pairs = {}
            self.plausible_reactive_car_trajectory_indices = []
            unseen_indices = [i for i in range(len(self.built_reactive_car_traj_list))]
            for i in NE_best_policies:
                if tuple(NE_policies[i]) not in self.policy_pairs:
                    self.policy_pairs[tuple(NE_policies[i])] = list(E_policies[i])
                    #Keep track of the trajectory choices of other that the equilibrium policies deem plausible
                    if unseen_indices!=[]:
                        for j,val in enumerate(NE_policies[i]):
                            if val>0 and j in unseen_indices:
                                self.plausible_reactive_car_trajectory_indices.append(j)
                                unseen_indices.remove(j)
                else:
                    self.policy_pairs[tuple(NE_policies[i])] = [x+y for x,y in zip(self.policy_pairs[tuple(NE_policies[i])],E_policies[i])]

        if self.debug: print("Plausible Trajectory indices for reactive car are: {}".format(self.plausible_reactive_car_trajectory_indices))

        #INSTEAD THE BEST POLICIES ARE WHAT THE NE_AGENT MIGHT DO, WE MUST COMPUTE THE BEST RESPONSE TO EACH ONE
        # GIVEN THE TRUE E REWARD FUNCTION, AND THEN THAT IS E'S BEHAVIOUR
        if self.ego_traj is  None:
            #if ego traj is None then Ego is choosing a trajectory to follow, and can choose from any trajectory
            ego_traj_choices = self.built_ego_traj_list
        else:
            #ottherwise Ego is already following a trajectory and can euther choose to continue with that
            # trajectory, or reverse it (i.e. cancel the manoeuvre and go back)
            ego_traj_choices = [self.ego_traj]
            if "Stopped" not in self.ego_traj.label and "Reversed" not in self.ego_traj.label:
                #ego_traj_choices.append(ReversedTrajectory(self.ego_traj,self.t-(1.5*self.ego.timestep)))
                #I think this will work. But only techincally
                #reversed_traj = self.traj_builder.makeTrajectory(self.ego_traj.label,self.ego_traj.state(self.t,self.ego.Lr+self.ego.Lf),dest_state=self.ego_traj.state(0,self.ego.Lr+self.ego.Lf),time_len=self.t)
                reversed_traj = self.traj_builder.makeTrajectory(self.ego_traj.label,self.ego.state,dest_state=self.ego_traj.state(0,self.ego.Lr+self.ego.Lf),time_len=self.t)
                reversed_traj.label+="-Reversed-{}".format(self.t)
                ego_traj_choices.append(reversed_traj)

            if "Stopped" not in self.ego_traj.label:
                ego_traj_choices.append(StoppedTrajectory(self.ego_traj,self.t))
            #if "Reversed" not in self.ego_traj.label:
            #ego_traj_choices = [self.ego_traj,ReversedTrajectory(self.ego_traj,self.t-(1.5*self.ego.timestep)),StoppedTrajectory(self.ego_traj,self.t)]
            #else:
                #Would prevent cyclical behaviour where the agent keeps switching between a trajectory and the reverse of that trajectory
            #    ego_traj_choices = [self.ego_traj,StoppedTrajectory(self.ego_traj,self.t)]

        #print("\nEgo trajectory choices are: {}".format([x.label for x in ego_traj_choices]))

        t4 = datetime.datetime.now()
        if self.debug: print("Time to determine E's trajectory choices is: {}".format((t4-t_cur).microseconds))
        t_cur = t4

        #Here we can calculate the Ego agent's true reward matrix based on the known trajectories available
        # to them and their known reward function
        #print("\n")
        E_cost_second_estimate = []

        reactive_car_time_distr = list(self.reactive_car_time_distr)
        #dummy_trajectories = [self.traj_builder.makeTrajectory(x,dummy_ego.state) for x in self.ego_traj_list]
        #Want to prevent the controller cancelling and then reinitialising the same trajectory for extra points
        #So we generate all possible next steps, then omit the one the agent is currently on (see "orig_index = ...")
        dummy = self.ego.copy()
        for ego_traj in ego_traj_choices:
            new_row = []
            #Omit the trajectroy the agent is currently on from consideration
            orig_label = re.findall("([a-zA-Z]+)",ego_traj.label)[0]
            orig_index = self.ego_traj_list.index(orig_label)
            putCarOnTraj(dummy,ego_traj,ego_traj.traj_len_t)
            possible_trajectories = [self.traj_builder.makeTrajectory(x,dummy.state) for i,x in enumerate(self.ego_traj_list) if i!=orig_index]
            reactive_car_t_offset = ego_traj.traj_len_t-self.t

            for i,reactive_car_traj in enumerate(self.built_reactive_car_traj_list):
                #we believe that the rational agent has no reason to perform these trajectories
                # no point wasting time computing the reward for them
                if i not in self.plausible_reactive_car_trajectory_indices: new_row.append(0)
                else:
                    #val,_ = computeReward(self.t,self.ego.copy(),ego_traj,self.other.copy(),other_traj,self.other_time_distr,veh1_reward_function=self.goal_function)
                    val_e,val_other = computeReward(self.t,self.ego.copy(),ego_traj,self.reactive_car.copy(),reactive_car_traj,self.reactive_car_time_distr,non_reactive_car=self.non_reactive_car.copy(),\
                            non_reactive_car_trajectories=self.built_non_reactive_car_traj_list,non_reactive_car_preference=self.non_reactive_car_preference,\
                            non_reactive_car_time_distr=self.non_reactive_car_time_distr,veh1_reward_function=self.goal_function)
                    #print("Reward to {} for performing {} when {} performs {} is {}".format(self.ego.label,ego_traj.label,self.other.label,other_traj.label,val_e))
                    if val_e != -1:
                        if ego_traj_choices != self.built_ego_traj_list:
                            #shift time forward by the appropriate amount
                            dummy_time_distr = reactive_car_time_distr[-1-int(reactive_car_t_offset/self.ego.timestep):] + reactive_car_time_distr[:-1-int(reactive_car_t_offset/self.ego.timestep)]
                            dummy_non_reactive_car_time_distr = self.non_reactive_car_time_distr[-1-int(reactive_car_t_offset/self.ego.timestep):] + self.non_reactive_car_time_distr[:-1-int(reactive_car_t_offset/self.ego.timestep)]
                            #max_possible_reward = max([computeReward(0,dummy_ego.copy(),x,dummy_other.copy(),other_traj,dummy_time_distr,veh1_reward_function=self.goal_function)[0] for x in dummy_trajectories])
                            possible_rewards = [computeReward(0,self.ego.copy(),x,self.reactive_car.copy(),reactive_car_traj,dummy_time_distr,self.non_reactive_car.copy(),\
                                    self.built_non_reactive_car_traj_list,self.non_reactive_car_preference,dummy_non_reactive_car_time_distr,veh1_reward_function=self.goal_function)[0] for x in possible_trajectories]
                            if self.debug: print("Possible Rewards for {} are: {}".format(ego_traj.label,possible_rewards))
                            max_possible_reward = max(possible_rewards)
                            #val_e += (.9**(ego_traj.traj_len_t-self.t))*max_possible_reward
                            val_e = max(val_e,(.9**(ego_traj.traj_len_t))*max_possible_reward)
                        #print("Reward to E ({}) for performing {} when NE ({}) performs {} is {}".format(self.ego.label,ego_traj.label,self.other.label,other_traj.label,val_e))
                        #new_row.append(val_other)
                        new_row.append(val_e)
                    else:
                        #print("E: {} NE: {} results in death for {}".format(ego_traj.label,other_traj.label,self.ego.label))
                        new_row.append(-1)
                        #print("Reward to ego for performing {} when NE performs {} is {}".format(ego_traj.label,other_traj.label,-1))
                    #new_row.append(val)

            E_cost_second_estimate.append(list(new_row))

        t5 = datetime.datetime.now()
        if self.debug: print("Time to determine reward for E's trajectories: {}".format((t5-t_cur).microseconds))
        t_cur = t5
        if self.debug:
            print("\n\nE's true estimate of the cost")
            for row in E_cost_second_estimate:
                print(row)
            print("\n")


        #expected_payoffs = []
        #for i,row in enumerate(E_cost_second_estimate):
        #    payoff = []
        #    for j,entry in enumerate(row):
        #        payoff.append(self.other_preference[j]*row[j])
        #    expected_payoffs.append(list(payoff))

        #print("\nE's expected payoff for each strategy")
        #for entry in expected_payoffs:
        #    print(entry)

        #ep_sum = [sum(row) for row in expected_payoffs]

        #We compute the expected payoff for each possible trajectory of E against each possible optimal
        # policy for NE
        expected_payoffs = []
        for policy in self.policy_pairs:
            payoff = []
            #Policy pairs identifies the different strategies/policies the other agent might use, and if they use it which policy/strategy they expect the ego agent to use
            other_does = policy #policy is  an ordered list of probabilities indicating the probability other will use each trajectory
            ego_does = self.policy_pairs[policy] # in practise policies are binary.
            #other follows the current policy only if they think the ego agent is going to follow the ego_does policy. Thus the probability the other agent is follow
            # the current policy is the sum of the probabilties of the trajectories that will trigger it. This should be normalised, but for the sake of computing expected
            # value it is fine.
            prob_other_policy = sum([self.ego_preference[i]*ego_does[i] for i in range(len(ego_does))])
            for j in range(len(ego_traj_choices)):
                ep = 0
                for i in range(len(other_does)):
                    #ep += NE_p[i]*E_cost_second_estimate[j][i]
                    #ep += self.other_preference[i]*NE_p[i]*E_cost_second_estimate[j][i]
                    ep += other_does[i]*self.reactive_car_preference[i]*E_cost_second_estimate[j][i]
                payoff += [prob_other_policy*ep]
            expected_payoffs.append(list(payoff))

        if self.debug:
            print("\nE's expected payoff for each strategy")
            for entry in expected_payoffs:
                print(entry)


        #We identify the trajectory for E that would have the highest expected value
        ep_sum = [0 for _ in ego_traj_choices]
        for row in expected_payoffs:
            for i,ep in enumerate(row):
                ep_sum[i] += ep

        max_index,max_ep = None,None
        for i,entry in enumerate(ep_sum):
            if max_index is None or entry>max_ep:
                max_index = i
                max_ep = entry

        chosen_traj = ego_traj_choices[max_index]

        if self.debug: print("\nChoosing {} with expected reward {}".format(chosen_traj.label,max_ep))
        if chosen_traj != self.ego_traj:
            if self.debug:
                if self.ego_traj is not None:
                    print("Ego proposing to change from {} to {}".format(self.ego_traj.label,chosen_traj.label))
                else:
                    print("Ego proposing to change to {}".format(chosen_traj.label))

            self.t = 0
            self.ego_traj = chosen_traj

            if self.debug:
                print("\nPrinting Proposed New Trajectory: {}".format(self.ego_traj.label))
                printTrajectory(self.ego,self.ego_traj,self.ego.timestep)

        t6 = datetime.datetime.now()
        if self.debug: print("Time to select action: {}".format((t6-t_cur).microseconds))
        t_cur = t6

        action = self.ego_traj.action(self.t,self.ego.Lf+self.ego.Lr)
        self.t += self.ego.timestep

        if self.debug: print("Selected Action is : {}".format(action))

        #print("\nReturning Accel: {}\tYaw Rate: {}\n".format(action[0],action[1]))
        if self.t>self.ego_traj.traj_len_t+2*self.ego.timestep:
            #print("\n{} Reached the end of the trajectory. Resetting Trajectory\n\n\n".format(self.ego.label))
            self.ego_traj = None
            self.t = 0

        if self.debug:
            print("Total time to compute actions is: {}".format((t6-t0).microseconds))
            print("\n\n")
        return action[0],action[1]


    def paramCopy(self,target=None):
        dup_initialisation_params = dict(self.initialisation_params)
        dup_initialisation_params["ego"] = target
        return dup_initialisation_params


    def copy(self,**kwargs):
        dup_init_params = self.paramCopy(**kwargs)
        return GameTheoryDrivingController(**dup_init_params)


    def updateStates(self):
        """Store the current state of the agents"""
        self.ego_state = dict(self.ego.state)
        self.reactive_car_state = dict(self.reactive_car.state)
        self.non_reactive_car_state = dict(self.non_reactive_car.state)


    def closeFiles(self):
        self.ego_preference_file.close()
        self.reactive_car_preference_file.close()
        self.rollout_file.close()


    def endStep(self):
        """Called (through the vehicle object) at the end of each iteration/timestep of the simulation.
           Allows information about how the iteration actually played out to be gathered"""
        #We use this function to determine the actions each agent actually chose
        #pass
        self.updateStates()
        if self.write:
            writeToFile(self.reactive_car_preference_file,self.reactive_car_preference)
            writeToFile(self.ego_preference_file,self.ego_preference)
            writeToFile(self.rollout_file,self.ego.state)
            #makes it easier to plot multiple files simultaneously if each writes to individual file
            #writeToFile(self.rollout_file,self.reactive_car.state)
            self.rollout_file.write("\n")
        #HERE WE WILL UPDATE COMPLIANCE (BETA)


def printTrajectory(veh,traj,timestep):
    time = 0
    position_list = traj.completePositionList(timestep)
    action_list = traj.completeActionList(veh.Lr+veh.Lf,timestep)
    print("{} positions\t{} actions".format(len(position_list),len(action_list)))
    for i,(posit,action) in enumerate(zip(position_list,action_list)):
        print("T: {}\tPosition: {}\tAction: {}".format(i*timestep,posit,action))

    print("\nEnd of Trajectory printout\n")


def initialiseFile(filename,ego_name,timestep,data_labels):
    date = datetime.datetime.now()
    file = open(filename+"-{}-{}_{}.txt".format(ego_name,date.hour,date.minute),"a")
    file.write("Ego is: {}\n".format(ego_name))
    file.write("Timestep Size: {}\n".format(timestep))
    if data_labels is not None:
        file.write("{}".format('\t'.join(data_labels)))
    file.write("\n")
    return file


def writeToFile(file,data):
    if isinstance(data,list):
        file.write("{}\n".format('\t'.join([str(x) for x in data])))
    elif isinstance(data,dict):
        file.write("{}\n".format(data))


def getState(veh,traj,t):
    state = {}
    #the action available to us is the last action taken
    # whereas the physical state available is the current state (time t)
    # therefore actions must be compared with t-1 traj state
    (state["acceleration"],state["yaw_rate"]) = traj.action(t-veh.timestep,veh.Lr+veh.Lf)
    state["position"] = traj.position(t)
    state["velocity"] = traj.velocity(t)
    state["heading"] = traj.heading(t)

    ret_state = {}
    if traj.relevant_features is None: return state
    else:
        for entry in traj.relevant_features:
            ret_state[entry] = state[entry]

        return ret_state


def getProb(veh,state,traj,t):
    target_state = getState(veh,traj,t)
    #print("For trajectory {}".format(traj.label))
    #print("Comparing Vehicle State: {}\n with Target State {}\n".format(state,target_state))
    prob = computeSimilarity(state,target_state)
    #print("Probability following {} is {}".format(traj.label,prob))
    return prob


def computeSimilarity(state,goal_state):
    sim = 0
    for entry in goal_state:
        sim += computeDistance(goal_state[entry],state[entry])**2
    sim = math.sqrt(sim)
    return math.exp(-sim)


def updatePreference(t,veh,veh_state,veh_traj_list,veh_pref):
    """We update the preferences of both agents based on the previous observations using a Bayes Filter"""
    new_pref = []
    num_sum = 0
    #state = veh.state
    state = veh_state
    #In the first iteration veh_state is {}, so can't update preference as we have no information
    if state != {}:
        #print("Original Preference profile is: {}".format(veh_pref))
        prob_state_given_traj = []
        for i,traj in enumerate(veh_traj_list):
            prob_state_given_traj.append(getProb(veh,state,traj,t))
        norm_sum = sum(prob_state_given_traj)
        prob_traj_given_state = [x/norm_sum for x in prob_state_given_traj]

        for i in range(len(veh_traj_list)):
            #new_pref_val = veh_pref[i]*getProb(veh,state,traj,t)
            new_pref_val = veh_pref[i]*prob_state_given_traj[i]
            num_sum += new_pref_val
            new_pref.append(new_pref_val)

        new_pref = [x/num_sum for x in new_pref]
    else:
        new_pref = list(veh_pref)
    #print("Updated Preference Profile is: {}".format(new_pref))
    return new_pref


def updateTimeDistr(veh,veh_state,traj_list,traj_preference,time_distr,dt):
    new_distr = []
    num_sum = 0
    #state is updated at the end of each iteration of the simulation. If state is {} then in first iteration
    # so can't update as we have no prior observations
    state = veh_state
    if state != {}:
        #P(t|state) = \sum_{t_old}\sum_{traj}P(t|t_old)*P(t_old|state}*P(traj|state)
        prob_t_given_t_old_complete = []
        for i in range(len(time_distr)):
            prob_t_given_t_old = []
            #Compute P(t|t_old)
            t_old = i*dt
            for j in range(len(traj_list)):
                traj = traj_list[j]
                prob_t_given_t_old_traj = []
                for k in range(len(time_distr)):
                    t = k*dt
                    #larger magnitude coefficient here results in a narrower distribution, which decays slower
                    prob_t_given_t_old_traj.append(math.exp(-5*abs(t-(t_old+dt)%(traj.traj_len_t+dt))/dt))
                norm_sum = sum(prob_t_given_t_old_traj)
                prob_t_given_t_old_traj = [x/norm_sum for x in prob_t_given_t_old_traj]
                prob_t_given_t_old.append(list(prob_t_given_t_old_traj))
            #print(f"If t_old is {t_old} then P(t|t_old) = {prob_t_given_t_old_traj}")
            prob_t_given_t_old_complete.append(list(prob_t_given_t_old))

        for i in range(len(time_distr)):
            #Compute P(t|state)
            new_distr_val = 0
            for j in range(len(traj_preference)):
                for k in range(len(time_distr)):
                    new_distr_val += prob_t_given_t_old_complete[k][j][i]*time_distr[k]*traj_preference[j]
            num_sum += new_distr_val
            new_distr.append(new_distr_val)
        new_distr = [x/num_sum for x in new_distr]
        #print("\nOld time distr was {}, new distr is: {}".format(time_distr,new_distr))
    else:
        new_distr = list(time_distr)

    return new_distr


def checkForLaneCrossing(veh):
    is_on = []
    for entry in [x for x in veh.on if isinstance(x,road_classes.Lane)]:
        if entry.label[:-1] in is_on:
            return True
        else:
            is_on.append(entry.label[:-1])
    return False


def checkForCrash(veh1,traj1,t1,veh2,traj2,t2,radius=-1):
    t =0
    r1,r2 = 0,0
    if radius == 1:
        radius = 2*computeDistance((veh2.x_com,veh2.y_com),veh2.four_corners["front_left"]) + .5
    while(t1+t<=traj1.traj_len_t and t2+t<=traj2.traj_len_t):
        putCarOnTraj(veh1,traj1,t1+t)
        putCarOnTraj(veh2,traj2,t2+t)

        #print("At time {} on {} traj {} and time {} on {} traj {}".format(init_t1+t,veh1.label,traj1.label,init_t2+t,veh2.label,traj2.label))
        if computeDistance((veh1.x_com,veh1.y_com),(veh2.x_com,veh2.y_com))<radius:
            #print("Cars have Crashed")
            r1 = -1
            r2 = -1
            break
        t += veh1.timestep

    return r1,r2


def computeReward(init_t1,veh1,traj1,veh2,traj2,veh2_time_distr,non_reactive_car=None,non_reactive_car_trajectories=None,non_reactive_car_preference=None,non_reactive_car_time_distr=None,veh1_reward_function=None,veh2_reward_function=None):
    #print("\nComputing Reward: {} performing: {}, {} Performing {}".format(veh1.label,traj1.label,veh2.label,traj2.label))
    init_veh1 = veh1.copy()
    init_veh2 = veh2.copy()

    r1 = 0
    r2 = 0

    init_t2 = min([i for i,prob in enumerate(veh2_time_distr) if prob == max(veh2_time_distr)])*veh2.timestep
    #print("Init_t1 is {}\tInit_t2 is: {}".format(init_t1,init_t2))

    #NOTE: COME BACK TO THIS LATER AND COME UP WITH A BETTER WAY OF ESTIMATING RADIUS. SHOULD INCREASE OVER TIME AS ESTIMATION CERTAINTY DECREASES
    radius = 2*computeDistance((veh2.x_com,veh2.y_com),veh2.four_corners["front_left"]) + .75

    r1,r2 = checkForCrash(veh1,traj1,init_t1,veh2,traj2,init_t2,radius)

    if r1 != -1:
        #print("No Crash found")

        putCarOnTraj(veh1,traj1,traj1.traj_len_t)
        putCarOnTraj(veh2,traj2,traj2.traj_len_t)
        #This is a bit sloppy as it doesn't account for the fact that a car might go off a road and back on during a trajectory. But for now we will allow it        
        #This also might require some refinement as it only works becausd teh copies cannot directly interact
        #print("E is on: {}".format([x.label for x in veh1.on]))
        #for entry in veh1.on:
        #    print("E: (Heading: {}) {}-{}\t{}: {}-{}".format(veh1.heading,veh1.four_corners["front_left"],veh1.four_corners["back_right"],entry.label,entry.four_corners["front_left"],entry.four_corners["back_right"]))
        #print("NE is on: {}".format([x.label for x in veh2.on]))
        #for entry in veh2.on:
        #    print("NE: (Heading: {}) {}-{}\t{}: {}-{}".format(veh2.heading,veh2.four_corners["front_left"],veh2.four_corners["back_right"],entry.label,entry.four_corners["front_left"],entry.four_corners["back_right"]))
        #print("\n")

        if veh1.on == [] or checkForLaneCrossing(veh1):
            #print("Veh {} has gone off road: Position: {}".format(veh1.label,veh1.state["position"]))
            #print(f"{veh1.label} is on {[x.label for x in veh1.on]}")
            r1 = -1 # not on anything => bad outcome
        if veh2.on == [] or checkForLaneCrossing(veh2):
            #print("Veh {} has gone off road: Position: {}".format(veh2.label,veh2.state["position"]))
            #print(f"{veh2.label} is on {[x.label for x in veh2.on]}")
            r2 = -1 #discourage stopping trajectory while crossing lanes

        if veh1_reward_function is not None and r1!=-1:
            r1 = veh1_reward_function(init_veh1,veh1)
        if veh2_reward_function is not None and r2!=-1:
            r2 = veh2_reward_function(init_veh2,veh2)

    if non_reactive_car is not None:
        #Checking for Collision with Non-Reactive Vehicle in the Environment
        new_r1,new_r2 = 0,0
        init_t_non_reactive = min([i for i,prob in enumerate(non_reactive_car_time_distr) if prob == max(non_reactive_car_time_distr)])*non_reactive_car.timestep
        for i,traj in enumerate(non_reactive_car_trajectories):
            if non_reactive_car_preference[i]>.01:
                if r1 != -1:
                    r1_val,_ = checkForCrash(veh1,traj1,init_t1,non_reactive_car,traj,init_t_non_reactive,radius)
                    new_r1 += r1_val*non_reactive_car_preference[i]

                if r2 != -1:
                    r2_val,_ = checkForCrash(veh2,traj2,init_t2,non_reactive_car,traj,init_t_non_reactive,radius)
                    new_r2 += r2_val*non_reactive_car_preference[i]

        r1 += new_r1
        r2 += new_r2

    #print("Returning: R1: {}\tR2: {}".format(r1,r2))
    return r1,r2


def computeGlobalCostMatrix(t,veh1,veh1_traj_list,veh2,veh2_traj_list,veh2_time_distr):
    E_cost_list = [[0 for _ in veh2_traj_list] for _ in veh1_traj_list]
    NE_cost_list = [[0 for _ in veh1_traj_list] for _ in veh2_traj_list]
    for i,E_traj in enumerate(veh1_traj_list):
        for j,NE_traj in enumerate(veh2_traj_list):
            #print("E doing: {}\tNE doing: {}".format(E_traj.label,NE_traj.label))
            E_cost_list[i][j],NE_cost_list[j][i] = computeReward(t,veh1.copy(),E_traj,veh2.copy(),NE_traj,veh2_time_distr)

    return E_cost_list,NE_cost_list


def computeNashEquilibria(E_cost_matrix,NE_cost_matrix,debug=False):
    E_cost_matrix = np.array(E_cost_matrix)
    NE_cost_matrix = np.array(NE_cost_matrix)
    game = nash.Game(E_cost_matrix,NE_cost_matrix)
    #equilibria = game.lemke_howson_enumeration()
    equilibria = game.support_enumeration(non_degenerate=False,tol=0)

    E_policies,NE_policies = [],[]
    for eq in equilibria:
        if not np.isnan(eq[0][0]):
            if debug: print("Eq is: {}".format(eq))
            E_policies.append(eq[0])
            NE_policies.append(eq[1])

    if debug:
        print("\nPrinting Equilibria policies")
        for i,(E,NE) in enumerate(zip(E_policies,NE_policies)):
            print("{}: {}\t{}".format(i,E,NE))
        print("End printing equilibria policies\n\n")

    return E_policies,NE_policies


def zeroFunction(*args):
    return 0


def computeDistance(pt1,pt2):
    if isinstance(pt1,numbers.Number) != isinstance(pt2,numbers.Number):
        print("GTCC Error: Points are not of the same type: PT1: {}\tPT2: {}".format(pt1,pt2))
        exit(-1)
    else:
        if isinstance(pt1,numbers.Number):
            return abs(pt1-pt2)
        else:
            return math.sqrt((pt2[0]-pt1[0])**2 + (pt2[1]-pt1[1])**2)
