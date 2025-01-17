from loss_writer import Writer
from learning_rate import LrHandler
from data_preprocess_and_load.dataloaders import DataHandler
import torch
import warnings
from tqdm import tqdm
from model import Encoder_Transformer_Decoder,Encoder_Transformer_finetune,AutoEncoder
from losses import get_intense_voxels
import time
import pathlib
import os

#DDP
from torch.utils.data.distributed import DistributedSampler
import torch.distributed as dist
from torch.nn import DataParallel
import builtins

class Trainer():
    """
    main class to handle training, validation and testing.
    note: the order of commands in the constructor is necessary
    """
    def __init__(self,sets,**kwargs):
        ############ get current last.pth ###########
        # directory = './experiments/'
        # for i in self.find_file(directory):
        #     if 'autoencoder' in i[0]:
        #         current_experiment_folder = i[0]
        #         break
        # current_model = self.find_file(os.path.join(directory, current_experiment_folder)+'/')[1][0]
        # model_absolute_path = directory+current_experiment_folder+'/'+current_model
        
#         directory = './experiments/'
#         if len(os.listdir(directory)) > 2:
#             sorted_folder_lst = self.find_file(directory)
#             print(sorted_folder_lst)
#             current_experiment_folder = sorted_folder_lst[1][0]
#             print('got current experiment folder!:', current_experiment_folder)
            
#             current_experiment_files_list = self.find_file(directory+current_experiment_folder+'/')
#             current_model_last_epoch = current_experiment_files_list[1][0]
#             print('got current model! its name is : {}'.format(current_model_last_epoch))
            
#             last_batch_idx = int(current_model_last_epoch.split('_')[-3])
#             print('got current model! whose last training batch index is : {}'.format(last_batch_idx))
            
#             model_absolute_path = directory+current_experiment_folder+'/'+current_model_last_epoch
#             self.first_training = False
            
#         else:
#             print('this is very first time for training')
#             last_batch_idx = -1
#             self.first_training = True
            
        ##############################################
        
        #self.last_batch_idx = last_batch_idx
        
        #self.loaded_model_weights_path = model_absolute_path
        # 이걸 epoch 9로 바꿔야 함.
        
        self.register_args(**kwargs)
        self.lr_handler = LrHandler(**kwargs)
        self.train_loader, self.val_loader, _ = DataHandler(**kwargs).create_dataloaders()
        self.create_model()
        #self.initialize_weights(load_cls_embedding=False)
        self.create_optimizer()
        self.lr_handler.set_schedule(self.optimizer)
        self.writer = Writer(sets,**kwargs) #여기서 이미 writer class를 불러옴.
        self.sets = sets
        self.eval_iter = 0
        self.batch_index = None
        self.best_loss = 100000
        self.best_accuracy = 0

        for name, loss_dict in self.writer.losses.items():
            if loss_dict['is_active']:
                print('using {} loss'.format(name))
                setattr(self, name + '_loss_func', loss_dict['criterion'])

    def create_optimizer(self):
        lr = self.lr_handler.base_lr
        params = self.model.parameters()
        weight_decay = self.kwargs.get('weight_decay')
        self.optimizer = torch.optim.Adam(params, lr=lr, weight_decay=weight_decay)

    def initialize_weights(self,load_cls_embedding):
        if self.loaded_model_weights_path is not None: #after autoencoder
            state_dict = torch.load(self.loaded_model_weights_path)
            self.lr_handler.set_lr(state_dict['lr'])
            self.model.module.load_partial_state_dict(state_dict['model_state_dict'],load_cls_embedding=False)
            self.model.module.loaded_model_weights_path = self.loaded_model_weights_path
            text = 'loaded model weights:\nmodel location - {}\nlast learning rate - {}\nvalidation loss - {}\n'.format(
                self.loaded_model_weights_path, state_dict['lr'],state_dict['loss_value'])
            if 'accuracy' in state_dict:
                text += 'validation accuracy - {}'.format(state_dict['accuracy'])
            print(text)
            

    def create_model(self):
        dim = self.train_loader.dataset.dataset.get_input_shape()
        if self.task.lower() == 'fine_tune':
            self.model = Encoder_Transformer_finetune(dim,**self.kwargs)
        elif self.task.lower() == 'autoencoder_reconstruction':
            self.model = AutoEncoder(dim,**self.kwargs)
        elif self.task.lower() == 'transformer_reconstruction':
            self.model = Encoder_Transformer_Decoder(dim,**self.kwargs)
            
        if self.distributed:
            # For multiprocessing distributed, DistributedDataParallel constructor
            # should always set the single device scope, otherwise,
            # DistributedDataParallel will use all available devices.
            if self.gpu is not None:
                self.device = torch.device('cuda:{}'.format(self.gpu))
                torch.cuda.set_device(self.gpu)
                self.model.cuda(self.gpu)
                self.model = torch.nn.parallel.DistributedDataParallel(self.model, device_ids=[self.gpu], broadcast_buffers=False, find_unused_parameters=True)
                net_without_ddp = self.model.module
            else:
                self.device = torch.device("cuda" if self.cuda else "cpu")
                self.model.cuda()
                self.model = torch.nn.parallel.DistributedDataParallel(self.model,find_unused_parameters=True)
                model_without_ddp = self.model.module
        else:
            self.device = torch.device("cuda" if self.cuda else "cpu")
            self.model = DataParallel(self.model).to(self.device)        
            
        torch.backends.cudnn.benchmark = True   
        

    def eval_epoch(self,set):
        loader = self.val_loader if set == 'val' else self.test_loader # 여기서 192473개 꺼내서 4등분 해서 1배치 당 48118개 뜨는 거임. 여기를 고치자!!
        start_time = time.time()
        self.eval(set)
        end_time = time.time()
        print('how much time takes to execute eval(): %20ds' % (end_time - start_time)) #이건 ㄹㅇ 금방함..
        with torch.no_grad(): #이야.. 이게 오래 걸리나보다.. 이걸 48118번 돌려야 하는 거임ㅋㅋ
            for input_dict in tqdm(loader, position=0, leave=True):
                loss_dict, _ = self.forward_pass(input_dict)
                #print('loss dict in eval_epoch:', loss_dict) # 이것도 48118개 하나씩 돌아감.. valid data loader에서 하나씩 꺼내오는 듯 하다.
                self.writer.write_losses(loss_dict, set=set) # 너는 어디에 저장되니?


    def testing(self):
        self.eval_epoch('test')
        self.writer.loss_summary(lr=0)
        self.writer.accuracy_summary(mid_epoch=False)
        for metric_name in dir(self.writer):
            if 'history' not in metric_name:
                continue
            metric_score = getattr(self.writer,metric_name)[-1]
            print('final test score - {} = {}'.format(metric_name,metric_score))


    def training(self):
        for epoch in range(self.nEpochs): #이게 10번 돌아야 한다는 말이지?
            self.train_epoch(epoch)
            self.eval_epoch('val')
            print('______epoch summary {}/{}_____\n'.format(epoch+1,self.nEpochs)) #이게 왜 프린트가 안 되었지? 아직 epoch를 못 돌았기 때문^^
            self.writer.loss_summary(lr=self.lr_handler.schedule.get_last_lr()[0])
            self.writer.accuracy_summary(mid_epoch=False)
            self.writer.save_history_to_csv()
            self.save_checkpoint_(epoch, len(self.train_loader)) #분명히 매 epoch마다 save checkpoint를 함.. 근데 왜 save가 안 되었냐? 아직 epoch를 못 돌았기 때문^^

    # 한 epoch 안에서 실행하는 함수 . 여기서 .pth를 불러와야 할 것 같음.
    def find_file (self, files_Path):
        file_name_and_time_lst = []
        for f_name in os.listdir(f"{files_Path}"):

            written_time = os.path.getctime(f"{files_Path}{f_name}")
            file_name_and_time_lst.append((f_name, written_time))
        # 생성시간 역순으로 정렬하고, 
        sorted_file_lst = sorted(file_name_and_time_lst, key=lambda x: x[1], reverse=True)

        return sorted_file_lst
    
    def train_epoch(self,epoch):
        
        if self.distributed:
            self.train_loader.sampler.set_epoch(epoch)
        
        #print('epoch is:', epoch) #epoch 아마 0으로 잡힐 것.
        #start_time = time.time()
        self.train()
        #end_time = time.time()
        #print('how much time takes to execute train(): %20ds' % (end_time - start_time))
        ### Stella added this ###

        #print('last batch index is:', self.last_batch_idx) #배치를 self.validation_frequency-1 까지 올리고 아래 코드 수행함.
        ### Stella added this ### 그럼 이제 14000부터 시작할 거임.
        
        for batch_idx, input_dict in enumerate(tqdm(self.train_loader,position=0,leave=True)): ### Stella changed this###
            # if batch_idx < self.last_batch_idx+1:
            #     batch_idx+=1
            # else:
            #     print('did batch index updated?', batch_idx) #13999부터 시작~~
            
            ### training ###
            self.writer.total_train_steps += 1
            self.optimizer.zero_grad()
            loss_dict, loss = self.forward_pass(input_dict)
            loss.backward()
            self.optimizer.step()
            self.lr_handler.schedule_check_and_update()
            self.writer.write_losses(loss_dict, set='train') # 이게 기록됨.

            if (batch_idx + 1) % self.validation_frequency == 0:
                print('batch index is:', batch_idx)
                #self.batch_idx = batch_idx
                

                ### validation ###
                start_time = time.time()
                self.eval_epoch('val') #이게 엄~청~ 오래 걸림 - 1 validation 당 6시간 15분 (인 지 아닌 지도 모름..^^)
                end_time = time.time() #여기까지 가지도 못 함.
                print('how much time takes to execute eval_epoch(): %20ds' % (end_time - start_time))
                #print('______mid-epoch summary {0:.2f}/{1:.0f}______\n'.format(partial_epoch,self.nEpochs))
                self.writer.loss_summary(lr=self.lr_handler.schedule.get_last_lr()[0])
                self.writer.accuracy_summary(mid_epoch=True)
                self.writer.experiment_title = self.writer.experiment_title
                self.writer.save_history_to_csv()
                
                self.save_checkpoint_(epoch, batch_idx)               
                self.train()
                


    def eval(self,set):
        self.mode = set
        self.model = self.model.eval()

    def train(self):
        self.mode = 'train'
        self.model = self.model.train()

    def get_last_loss(self):
        if self.kwargs.get('fine_tune_task') == 'regression': #self.model.task
            return self.writer.val_MAE[-1]
        else:
            return self.writer.total_val_loss_history[-1]

    def get_last_accuracy(self):
        if hasattr(self.writer,'val_AUROC'):
            return self.writer.val_AUROC[-1]
        else:
            return None

    def save_checkpoint_(self, epoch, batch_idx):
        partial_epoch = epoch + (batch_idx / len(self.train_loader))
        
        print('in save_checkpoint_ function, epoch is:', partial_epoch)
        loss = self.get_last_loss()
        accuracy = self.get_last_accuracy()
        title = str(self.writer.experiment_title) + '_epoch_' + str(int(epoch)) + '_batch_index_'+ str(batch_idx) # 이 함수 안에서만 쓰도록 함~
        self.save_checkpoint(
            self.writer.experiment_folder, title, partial_epoch, loss ,accuracy, self.optimizer ,schedule=self.lr_handler.schedule) #experiments에 저장
        
    
    def save_checkpoint(self, directory, title, epoch, loss, accuracy, optimizer=None,schedule=None):
        # Create directory to save to
        if not os.path.exists(directory):
            os.makedirs(directory)

        # Build checkpoint dict to save.
        ckpt_dict = {
            'model_state_dict':self.model.state_dict(),
            'optimizer_state_dict':optimizer.state_dict() if optimizer is not None else None,
            'epoch':epoch,
            'loss_value':loss}
        if accuracy is not None:
            ckpt_dict['accuracy'] = accuracy
        if schedule is not None:
            ckpt_dict['schedule_state_dict'] = schedule.state_dict()
            ckpt_dict['lr'] = schedule.get_last_lr()[0]
        if hasattr(self,'loaded_model_weights_path'):
            ckpt_dict['loaded_model_weights_path'] = self.loaded_model_weights_path
        
        # Save checkpoint per one epoch 
        # core_name = title
        # print('saving ckpt of {}_epoch'.format(epoch))
        # name = "{}_epoch_{}.pth".format(core_name, epoch)
        # torch.save(ckpt_dict, os.path.join(directory, name))
        
        # Save the file with specific name
        core_name = title
        name = "{}_last_epoch.pth".format(core_name) # (2) 아... last epoch에서만 저장이..되는거야..?^^..?
        torch.save(ckpt_dict, os.path.join(directory, name)) # (1) 그래서 이 last epoch 모델이 왜 experiments 디렉토리에 저장이 안 되냐 이거지
        
        # best loss나 best accuracy를 가진 모델만 저장하는 코드
        if self.best_loss > loss:
            self.best_loss = loss
            name = "{}_BEST_val_loss.pth".format(core_name)
            torch.save(ckpt_dict, os.path.join(directory, name))
            print('updating best saved model...')
        if accuracy is not None and self.best_accuracy < accuracy:
            self.best_accuracy = accuracy
            name = "{}_BEST_val_accuracy.pth".format(core_name)
            torch.save(ckpt_dict, os.path.join(directory, name))
            print('updating best saved model...')


    def forward_pass(self,input_dict):
        input_dict = {k:(v.to(self.gpu) if self.cuda else v) for k,v in input_dict.items()}
        #print('shape of input dict is :', input_dict['fmri_sequence'].size())
        output_dict = self.model(input_dict['fmri_sequence'])
        loss_dict, loss = self.aggregate_losses(input_dict, output_dict)
        if self.task == 'fine_tune':
            self.compute_accuracy(input_dict, output_dict)
        return loss_dict, loss


    def aggregate_losses(self,input_dict,output_dict):
        final_loss_dict = {}
        final_loss_value = 0
        for loss_name, current_loss_dict in self.writer.losses.items():
            if current_loss_dict['is_active']:
                loss_func = getattr(self, 'compute_' + loss_name)
                current_loss_value = loss_func(input_dict,output_dict)
                if current_loss_value.isnan().sum() > 0:
                    warnings.warn('found nans in computation')
                    print('at {} loss'.format(loss_name))
                lamda = current_loss_dict['factor']
                factored_loss = current_loss_value * lamda
                final_loss_dict[loss_name] = factored_loss.item()
                final_loss_value += factored_loss
        final_loss_dict['total'] = final_loss_value.item()
        return final_loss_dict, final_loss_value

    def compute_reconstruction(self,input_dict,output_dict):
        fmri_sequence = input_dict['fmri_sequence'][:,0].unsqueeze(1)
        reconstruction_loss = self.reconstruction_loss_func(output_dict['reconstructed_fmri_sequence'],fmri_sequence)
        return reconstruction_loss

    def compute_intensity(self,input_dict,output_dict):
        per_voxel = input_dict['fmri_sequence'][:,1,:,:,:,:]
        voxels = get_intense_voxels(per_voxel, output_dict['reconstructed_fmri_sequence'].shape)
        output_intense = output_dict['reconstructed_fmri_sequence'][voxels]
        truth_intense = input_dict['fmri_sequence'][:,0][voxels.squeeze(1)]
        intensity_loss = self.intensity_loss_func(output_intense.squeeze(), truth_intense)
        return intensity_loss

    def compute_perceptual(self,input_dict,output_dict):
        fmri_sequence = input_dict['fmri_sequence'][:,0].unsqueeze(1)
        perceptual_loss = self.perceptual_loss_func(output_dict['reconstructed_fmri_sequence'],fmri_sequence)
        return perceptual_loss

    def compute_binary_classification(self,input_dict,output_dict):
        binary_loss = self.binary_classification_loss_func(output_dict['binary_classification'].squeeze(), input_dict['subject_binary_classification'].squeeze())
        return binary_loss

    def compute_regression(self,input_dict,output_dict):
        gender_loss = self.regression_loss_func(output_dict['regression'].squeeze(),input_dict['subject_regression'].squeeze())
        return gender_loss

    def compute_accuracy(self,input_dict,output_dict):
        task = self.kwargs.get('fine_tune_task') #self.model.task
        out = output_dict[task].detach().clone().cpu()
        score = out.squeeze() if out.shape[0] > 1 else out
        labels = input_dict['subject_' + task].clone().cpu()
        subjects = input_dict['subject'].clone().cpu()
        for i, subj in enumerate(subjects):
            subject = str(subj.item())
            if subject not in self.writer.subject_accuracy:
                self.writer.subject_accuracy[subject] = {'score': score[i].unsqueeze(0), 'mode': self.mode, 'truth': labels[i],'count': 1}
            else:
                self.writer.subject_accuracy[subject]['score'] = torch.cat([self.writer.subject_accuracy[subject]['score'], score[i].unsqueeze(0)], dim=0)
                self.writer.subject_accuracy[subject]['count'] += 1

    def register_args(self,**kwargs):
        for name,value in kwargs.items():
            setattr(self,name,value)
        self.kwargs = kwargs


