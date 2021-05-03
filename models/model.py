import copy
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import utility.utils as utils
from math import floor

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

# Implementation of Twin Delayed Deep Deterministic Policy Gradients (TD3)
# Paper: https://arxiv.org/abs/1802.09477
# Original Implementation found on https://github.com/sfujim/TD3/blob/master/TD3.py

class FirstBlock(nn.Module):
    def __init__(self, in_channel, out_channel):
        super().__init__()
        self.conv = nn.Conv2d(in_channel, out_channel, kernel_size=3, stride=1, padding=1, bias=False)
        torch.nn.init.kaiming_normal_(self.conv.weight, mode='fan_in')
        self.bn = nn.BatchNorm2d(out_channel)
        self.prelu = nn.PReLU()
        
    def forward(self, x):
        out = self.conv(x)
        out = self.bn(out)
        out = self.prelu(out)
        return out    

    
class InnerBlock(nn.Module):
    def __init__(self, in_channel, out_channel, stride=1):
        super().__init__()
        assert stride == 1 or stride == 2
        self.conv = nn.Conv2d(in_channel, out_channel, kernel_size=3, stride=stride, padding=1)
        torch.nn.init.kaiming_normal_(self.conv.weight, mode='fan_out')
        self.bn = nn.BatchNorm2d(out_channel)
        
        self.conv2 = nn.Conv2d(out_channel, out_channel, kernel_size=3, stride=1, padding=1)
        torch.nn.init.kaiming_normal_(self.conv2.weight, mode='fan_out')
        self.bn2 = nn.BatchNorm2d(out_channel)
        
        self.prelu = nn.PReLU()
        
        if stride == 1 and in_channel == out_channel:
            self.shortcut = nn.Identity()
        else:            
            self.shortcut = nn.Conv2d(in_channel, out_channel, kernel_size=1, stride=stride)
            torch.nn.init.kaiming_normal_(self.shortcut.weight, mode='fan_out')

            
    def forward(self, x):
        out = self.conv(x)
        out = self.bn(out)
        out = self.prelu(out)
        
        out = self.conv2(out)
        out = self.bn2(out)
        out = self.prelu(self.shortcut(x) + out)
        return out   


class CNN(nn.Module):
    def __init__(
        self,
        indicator_state_dim,
        immediate_state_dim,
        outchannel,
        activation=nn.PReLU,
    ):
        super(CNN, self).__init__()
        self.layers = nn.Sequential(
            FirstBlock(1, 32),

            InnerBlock(32, 64), 
            InnerBlock(64, 64),

            InnerBlock(64, 64, stride=2), 
            InnerBlock(64, 128),
            InnerBlock(128, 128),

            InnerBlock(128, 256, stride=2), 
            InnerBlock(256, 256), 
            InnerBlock(256, 256), 

            InnerBlock(256, 512, stride=2), 
            InnerBlock(512, 512), 
            InnerBlock(512, 512), 
            nn.AdaptiveAvgPool2d((1, 1))
        )
       
        self.layers2 = nn.Sequential(
            FirstBlock(1, 32),

            InnerBlock(32, 64), 
            InnerBlock(64, 64),

            InnerBlock(64, 128, stride=2), 
            InnerBlock(128, 128),
            InnerBlock(128, 128),

            InnerBlock(128, 256, stride=2), 
            InnerBlock(256, 256), 
            InnerBlock(256, 256), 

            InnerBlock(256, 512, stride=2), 
            InnerBlock(512, 512), 
            InnerBlock(512, 512), 

            nn.AdaptiveAvgPool2d((1, 1))
        )       

        self.flatten = nn.Flatten()
        self.output = nn.Linear(512 + 512,  outchannel)


    def forward(self, X, X_immediate):
        out = self.layers(X.unsqueeze(1))   
        out = self.flatten(out)

        out2 = self.layers2(X_immediate.unsqueeze(1))
        out2 = self.flatten(out2)

        out = self.output(torch.cat((out, out2), -1))
        return out


class Actor(nn.Module):
    def __init__(self, ind_state_dim, imm_state_dim, action_dim, max_action):
        super(Actor, self).__init__()
        self.conv = CNN(ind_state_dim, imm_state_dim, 124)
        self.l1 = nn.Linear(124, 124)
        self.l2 = nn.Linear(124, 124)
        self.l3 = nn.Linear(124, action_dim)

        self.prelu = nn.PReLU()
        self.max_action = max_action

    def forward(self, ind_state, imm_state):
        ind_state = self.conv(ind_state, imm_state)
        a = self.prelu(self.l1(ind_state))
        a = self.prelu(self.l2(a))
        a = self.max_action * torch.tanh(self.l3(a))
        return a


class Critic(nn.Module):
    def __init__(self, indicator_state_dim, immediate_state_dim, action_dim):
        super(Critic, self).__init__()
        # Q1 architecture
        self.cnn = CNN(indicator_state_dim, immediate_state_dim, 124)
        self.l1 = nn.Linear(124 + action_dim, 124)
        self.l2 = nn.Linear(124, 124)
        self.l3 = nn.Linear(124, 1)
        # Q2 architecture
        self.cnn2 = CNN(indicator_state_dim, immediate_state_dim, 124)
        self.l4 = nn.Linear(124 + action_dim, 124)
        self.l5 = nn.Linear(124, 124)
        self.l6 = nn.Linear(124, 1)

        self.prelu = nn.PReLU()

    def forward(self, indicator_state, immediate_state, action):
        sa1 = self.cnn(indicator_state, immediate_state)
        sa1 = torch.cat([sa1, action], 1)
        q1 = self.prelu(self.l1(sa1))
        q1 = self.prelu(self.l2(q1))
        q1 = self.l3(q1)
        sa2 = self.cnn2(indicator_state, immediate_state)
        sa2 = torch.cat([sa2, action], 1)
        q2 = self.prelu(self.l4(sa2))
        q2 = self.prelu(self.l5(q2))
        q2 = self.l6(q2)
        return q1, q2

    def Q1(self, indicator_state_dim, immediate_state, action):
        sa = self.cnn(indicator_state_dim, immediate_state)
        sa = torch.cat([sa, action], 1)
        q1 = self.prelu(self.l1(sa))
        q1 = self.prelu(self.l2(q1))
        q1 = self.l3(q1)
        return q1


class TD3(object):
    def __init__(
        self,
        state_dim,
        action_dim,
        max_action,
        discount=0.99,
        tau=0.005,
        policy_noise=0.2,
        noise_clip=0.5,
        policy_freq=2,
        lr=3e-4,
    ):
        indicator_state_dim, immediate_state_dim = state_dim
        self.actor = Actor(
            indicator_state_dim, immediate_state_dim, action_dim, max_action
        ).to(device)
        self.actor_target = copy.deepcopy(self.actor)
        self.actor_optimizer = torch.optim.Adam(self.actor.parameters(), lr=lr)
        self.critic = Critic(indicator_state_dim, immediate_state_dim, action_dim).to(
            device
        )
        self.critic_target = copy.deepcopy(self.critic)
        self.critic_optimizer = torch.optim.Adam(self.critic.parameters(), lr=lr)
        self.max_action = max_action
        self.discount = discount
        self.tau = tau
        self.policy_noise = policy_noise
        self.noise_clip = noise_clip
        self.policy_freq = policy_freq
        self.total_it = 0

        self.actor_scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(self.actor_optimizer, factor=0.5, patience=20,  verbose=True)
        self.critic_scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(self.critic_optimizer, factor=0.5, patience=20,  verbose=True)

    def select_action(self, state_tup):
        ind_state, imm_state = state_tup
        if len(ind_state.shape) == 2:
            ind_state = torch.FloatTensor([ind_state]).to(device)
            imm_state = torch.FloatTensor([imm_state]).to(device)
        else:
            ind_state = torch.FloatTensor(ind_state).to(device)
            imm_state = torch.FloatTensor(imm_state).to(device)
        action = self.actor(ind_state, imm_state).cpu().data.numpy()
        return action

    def train(self, replay_buffer, batch_size=100):
        self.total_it += 1
        # Sample replay buffer
        (
            ind_state,
            imm_state,
            action,
            next_ind_state,
            next_imm_state,
            reward,
            not_done,
        ) = replay_buffer.sample(batch_size)

        with torch.no_grad():
            # Select action according to policy
            noise = (torch.randn_like(action) * self.policy_noise).clamp(
                -self.noise_clip, self.noise_clip
            )
            next_action = (
                self.actor_target(next_ind_state, next_imm_state) + noise
            ).clamp(-self.max_action, self.max_action)
            # Compute the target Q value
            target_Q1, target_Q2 = self.critic_target(
                next_ind_state, next_imm_state, next_action
            )
            target_Q = torch.min(target_Q1, target_Q2)
            target_Q = reward + not_done * self.discount * target_Q
        # Get current Q estimates
        current_Q1, current_Q2 = self.critic(ind_state, imm_state, action)
        # Compute critic loss
        critic_loss = F.mse_loss(current_Q1, target_Q) + F.mse_loss(
            current_Q2, target_Q
        )
        # Optimize the critic
        self.critic_optimizer.zero_grad()
        critic_loss.backward()
        torch.nn.utils.clip_grad_norm_(self.critic.parameters(), 1) 
        self.critic_optimizer.step()

        # Delayed policy updates
        if self.total_it % self.policy_freq == 0:
            # Compute actor losse
            actor_loss = -self.critic.Q1(
                ind_state, imm_state, self.actor(ind_state, imm_state),
            ).mean()
            # Optimize the actor
            self.actor_optimizer.zero_grad()

            actor_loss.backward()
            torch.nn.utils.clip_grad_norm_(self.actor.parameters(), 1) 

            self.actor_optimizer.step()
            # self.actor_scheduler.step(actor_loss)
            # Update the frozen target models
            for param, target_param in zip(
                self.critic.parameters(), self.critic_target.parameters()
            ):
                target_param.data.copy_(
                    self.tau * param.data + (1 - self.tau) * target_param.data
                )
            for param, target_param in zip(
                self.actor.parameters(), self.actor_target.parameters()
            ):
                target_param.data.copy_(
                    self.tau * param.data + (1 - self.tau) * target_param.data
                )
    

    def save(self, filename):
        torch.save(self.critic.state_dict(), filename + "_critic")
        torch.save(self.critic_optimizer.state_dict(), filename + "_critic_optimizer")
        torch.save(self.actor.state_dict(), filename + "_actor")
        torch.save(self.actor_optimizer.state_dict(), filename + "_actor_optimizer")

    def load(self, filename):
        self.critic.load_state_dict(torch.load(filename + "_critic"))
        self.critic_optimizer.load_state_dict(
            torch.load(filename + "_critic_optimizer")
        )
        self.critic_target = copy.deepcopy(self.critic)
        self.actor.load_state_dict(torch.load(filename + "_actor"))
        self.actor_optimizer.load_state_dict(torch.load(filename + "_actor_optimizer"))
        self.actor_target = copy.deepcopy(self.actor)


class ReplayBuffer(object):
    def __init__(self, state_dim, action_dim, max_size=int(1e6)):
        indicator_state, immediate_state = state_dim
        self.max_size = max_size
        self.ptr = 0
        self.size = 0
        if type(indicator_state) == int:
            full_indicator_state = [max_size] + [indicator_state]
        else:
            full_indicator_state = [max_size] + [s for s in indicator_state]
        if type(immediate_state) == int:
            full_immediate_state = [max_size] + [immediate_state]
        else:
            full_immediate_state = [max_size] + [s for s in immediate_state]
        if type(action_dim) == int:
            full_action_dim = [max_size] + [action_dim]
        else:
            full_action_dim = [max_size] + [a for a in action_dim]
        self.action = np.zeros(full_action_dim)
        self.indicator_state = np.zeros(full_indicator_state)
        self.immediate_state = np.zeros(full_immediate_state)
        self.next_indicator_state = np.zeros(full_indicator_state)
        self.next_immediate_state = np.zeros(full_immediate_state)
        self.reward = np.zeros((max_size, 1))
        self.not_done = np.zeros((max_size, 1))
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    def add(self, state, action, next_state, reward, done):
        indicator_state, immediate_state = state
        next_indicator_state, next_immediate_state = next_state
        self.immediate_state[self.ptr] = immediate_state
        self.indicator_state[self.ptr] = indicator_state
        self.action[self.ptr] = action
        self.next_immediate_state[self.ptr] = next_immediate_state
        self.next_indicator_state[self.ptr] = next_indicator_state
        self.reward[self.ptr] = reward
        self.not_done[self.ptr] = 1.0 - done
        self.ptr = (self.ptr + 1) % self.max_size
        self.size = min(self.size + 1, self.max_size)

    def sample(self, batch_size):
        ind = np.random.randint(0, self.size, size=batch_size)
        return (
            torch.FloatTensor(self.indicator_state[ind]).to(self.device),
            torch.FloatTensor(self.immediate_state[ind]).to(self.device),
            torch.FloatTensor(self.action[ind]).to(self.device),
            torch.FloatTensor(self.next_indicator_state[ind]).to(self.device),
            torch.FloatTensor(self.next_immediate_state[ind]).to(self.device),
            torch.FloatTensor(self.reward[ind]).to(self.device),
            torch.FloatTensor(self.not_done[ind]).to(self.device),
        )

