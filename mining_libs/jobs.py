import binascii
import time
import struct
import subprocess
import weakref

from twisted.internet import defer

import utils

import stratum.logger
log = stratum.logger.get_logger('proxy')

class Job(object):
    def __init__(self):
        self.job_id = None
        self.prevhash = ''
        self.coinb1_bin = ''
        self.coinb2_bin = ''
        self.merkle_branch = []
        self.version = 1
        self.nbits = 0
        self.ntime_delta = 0
        self.diff = 1
        self.extranonce2 = 0
        self.merkle_to_extranonce2 = {} # Relation between merkle_hash and extranonce2

    @classmethod
    def build_from_broadcast(cls, job_id, prevhash, coinb1, coinb2, merkle_branch, version, nbits, ntime, diff):
        '''Build job object from Stratum server broadcast'''
        job = Job()
        job.job_id = job_id
        job.prevhash = prevhash
        job.coinb1_bin = binascii.unhexlify(coinb1)
        job.coinb2_bin = binascii.unhexlify(coinb2)
        job.merkle_branch = [ binascii.unhexlify(tx) for tx in merkle_branch ]
        job.version = version
        job.nbits = nbits
        job.ntime_delta = int(ntime, 16) - int(time.time())
        job.diff = diff
        return job

    def increase_extranonce2(self):
        self.extranonce2 += 1
        return self.extranonce2

    def build_coinbase(self, extranonce):
        return self.coinb1_bin + extranonce + self.coinb2_bin
    
    def build_merkle_root(self, coinbase_hash):
        merkle_root = coinbase_hash
        for h in self.merkle_branch:
            merkle_root = utils.doublesha(merkle_root + h)
        return merkle_root
    
    def serialize_header(self, merkle_root, ntime, nonce):
        r =  self.version
        r += self.prevhash
        r += merkle_root
        r += binascii.hexlify(struct.pack(">I", ntime))
        r += self.nbits
        r += binascii.hexlify(struct.pack(">I", nonce))
        r += '000000800000000000000000000000000000000000000000000000000000000000000000000000000000000080020000' # padding    
        return r            
        
class JobRegistry(object):   
    def __init__(self, f, cmd, scrypt_target=False):
        self.f = f
        self.cmd = cmd # execute this command on new block
        self.scrypt_target = scrypt_target # calculate target for scrypt algorithm instead of sha256
        self.jobs = []        
        self.last_job = None
        self.extranonce1 = None
        self.extranonce1_bin = None
        self.extranonce2_size = None
        
        self.target = 0
        self.target_hex = ''
        self.difficulty = 1
        self.set_difficulty(1)
        self.target1_hex = self.target_hex
        
        # Relation between merkle and job
        self.merkle_to_job= weakref.WeakValueDictionary()
        
        # Hook for LP broadcasts
        self.on_block = defer.Deferred()

    def execute_cmd(self, prevhash):
        if self.cmd:
            return subprocess.Popen(self.cmd.replace('%s', prevhash), shell=True)

    def set_extranonce(self, extranonce1, extranonce2_size):
        self.extranonce2_size = extranonce2_size
        self.extranonce1_bin = binascii.unhexlify(extranonce1)
        self.extranonce1 = extranonce1
        log.info("Set extranonce: %s/%s", self.extranonce1, self.extranonce2_size)
        
    def set_difficulty(self, new_difficulty):
        if self.scrypt_target:
            dif1 = 0x0000ffff00000000000000000000000000000000000000000000000000000000
        else:
            dif1 = 0x00000000ffff0000000000000000000000000000000000000000000000000000
        self.target = int(dif1 / new_difficulty)
        self.target_hex = binascii.hexlify(utils.uint256_to_str(self.target))
        self.difficulty = new_difficulty
 
    def build_full_extranonce(self, extranonce2):
        '''Join extranonce1 and extranonce2 together while padding
        extranonce2 length to extranonce2_size (provided by server).'''        
        return self.extranonce1_bin + self.extranonce2_padding(extranonce2)

    def extranonce2_padding(self, extranonce2):
        '''Return extranonce2 with padding bytes'''

        if not self.extranonce2_size:
            raise Exception("Extranonce2_size isn't set yet")
        
        extranonce2_bin = struct.pack('>I', extranonce2)
        missing_len = self.extranonce2_size - len(extranonce2_bin)
        
        if missing_len < 0:
            # extranonce2 is too long, we should print warning on console,
            # but try to shorten extranonce2 
            log.info("Extranonce size mismatch. Please report this error to pool operator!")
            return extranonce2_bin[abs(missing_len):]

        # This is probably more common situation, but it is perfectly
        # safe to add whitespaces
        return '\x00' * missing_len + extranonce2_bin 
    
    def add_template(self, template, clean_jobs):
        if clean_jobs:
            # Pool asked us to stop submitting shares from previous jobs
            self.jobs = []
            
        self.jobs.append(template)
        self.last_job = template
                
        if clean_jobs:
            # Force miners to reload jobs
            on_block = self.on_block
            self.on_block = defer.Deferred()
            on_block.callback(True)
    
            # blocknotify-compatible call
            self.execute_cmd(template.prevhash)
          
    def register_merkle(self, job, merkle_hash, extranonce2):
        # merkle_to_job is weak-ref, so it is cleaned up automatically
        # when job is dropped
        self.merkle_to_job[merkle_hash] = job
        job.merkle_to_extranonce2[merkle_hash] = extranonce2
        
    def get_job_from_header(self, header):
        '''Lookup for job and extranonce2 used for given blockheader (in hex)'''
        merkle_hash = header[72:136].lower()
        job = self.merkle_to_job[merkle_hash]
        extranonce2 = job.merkle_to_extranonce2[merkle_hash]
        return (job, extranonce2)

    def get_job_from_id(self,job_id):
        job = None
        for j in self.jobs:
            if j.job_id == job_id:
                job = j
                break
        return job
