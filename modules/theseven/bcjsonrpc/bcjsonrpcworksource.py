# Modular Python Bitcoin Miner
# Copyright (C) 2012 Michael Sparmann (TheSeven)
#
#     This program is free software; you can redistribute it and/or
#     modify it under the terms of the GNU General Public License
#     as published by the Free Software Foundation; either version 2
#     of the License, or (at your option) any later version.
#
#     This program is distributed in the hope that it will be useful,
#     but WITHOUT ANY WARRANTY; without even the implied warranty of
#     MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#     GNU General Public License for more details.
#
#     You should have received a copy of the GNU General Public License
#     along with this program; if not, write to the Free Software
#     Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.
#
# Please consider donating to 1PLAPWDejJPJnY2ppYCgtw5ko8G5Q4hPzh if you
# want to support further development of the Modular Python Bitcoin Miner.



#######################################
# Bitcoin JSON RPC work source module #
#######################################



import time
import json
import struct
import base64
import traceback
from binascii import hexlify, unhexlify
from threading import Thread, RLock, Condition
from core.actualworksource import ActualWorkSource
from core.job import Job
try: from queue import Queue
except: from Queue import Queue
try: import http.client as http_client
except ImportError: import httplib as http_client



class BCJSONRPCWorkSource(ActualWorkSource):
  
  version = "theseven.bcjsonrpc work source v0.1.0beta"
  default_name = "Untitled BCJSONRPC work source"
  settings = dict(ActualWorkSource.settings, **{
    "getworktimeout": {"title": "Getwork timeout", "type": "float", "position": 19000},
    "sendsharetimeout": {"title": "Sendshare timeout", "type": "float", "position": 19100},
    "longpolltimeout": {"title": "Long poll connect timeout", "type": "float", "position": 19200},
    "longpollresponsetimeout": {"title": "Long poll response timeout", "type": "float", "position": 19200},
    "host": {"title": "Host", "type": "string", "position": 1000},
    "port": {"title": "Port", "type": "int", "position": 1010},
    "path": {"title": "Path", "type": "string", "position": 1020},
    "username": {"title": "User name", "type": "string", "position": 1100},
    "password": {"title": "Password", "type": "password", "position": 1120},
    "useragent": {"title": "User agent string", "type": "string", "position": 1200},
    "getworkconnections": {"title": "Job fetching connnections", "type": "int", "position": 1300},
    "uploadconnections": {"title": "Share upload connnections", "type": "int", "position": 1400},
    "longpollconnections": {"title": "Long poll connnections", "type": "int", "position": 1500},
    "expirymargin": {"title": "Job expiry safety margin", "type": "int", "position": 1600},
  })
  

  def __init__(self, core, state = None):
    self.fetcherlock = Condition()
    self.fetcherthreads = []
    self.fetchersrunning = 0
    self.fetcherspending = 0
    self.fetcherjobsrunning = 0
    self.fetcherjobspending = 0
    self.uploadqueue = Queue()
    self.uploaderthreads = []
    super(BCJSONRPCWorkSource, self).__init__(core, state)
    self.extensions = "longpoll midstate rollntime"
    self.runcycle = 0
    
    
  def apply_settings(self):
    super(BCJSONRPCWorkSource, self).apply_settings()
    if not "getworktimeout" in self.settings or not self.settings.getworktimeout:
      self.settings.getworktimeout = 3
    if not "sendsharetimeout" in self.settings or not self.settings.sendsharetimeout:
      self.settings.sendsharetimeout = 5
    if not "longpolltimeout" in self.settings or not self.settings.longpolltimeout:
      self.settings.longpolltimeout = 10
    if not "longpollresponsetimeout" in self.settings or not self.settings.longpollresponsetimeout:
      self.settings.longpollresponsetimeout = 1800
    if not "host" in self.settings: self.settings.host = ""
    if self.started and self.settings.host != self.host: self.async_restart()
    if not "port" in self.settings or not self.settings.port: self.settings.port = 8332
    if self.started and self.settings.port != self.port: self.async_restart()
    if not "path" in self.settings or not self.settings.path:
      self.settings.path = "/"
    if not "username" in self.settings: self.settings.username = ""
    if not "password" in self.settings: self.settings.password = ""
    if not self.settings.username and not self.settings.password: self.auth = None
    else:
      credentials = self.settings.username + ":" + self.settings.password
      self.auth = "Basic " + base64.b64encode(credentials.encode("utf_8")).decode("ascii")
    if not "useragent" in self.settings: self.settings.useragent = ""
    if self.settings.useragent: self.useragent = self.settings.useragent
    else: self.useragent = "%s (%s)" % (self.core.__class__.version, self.__class__.version)
    if not "getworkconnections" in self.settings: self.settings.getworkconnections = 1
    if self.started and self.settings.getworkconnections != self.getworkconnections: self.async_restart()
    if not "uploadconnections" in self.settings: self.settings.uploadconnections = 1
    if self.started and self.settings.uploadconnections != self.uploadconnections: self.async_restart()
    if not "longpollconnections" in self.settings: self.settings.longpollconnections = 1
    if self.started and self.settings.longpollconnections != self.longpollconnections: self.async_restart()
    if not "expirymargin" in self.settings: self.settings.expirymargin = 5

    
  def _reset(self):
    super(BCJSONRPCWorkSource, self)._reset()
    self.stats.supports_rollntime = None
    self.longpollurl = None
    self.fetchersrunning = 0
    self.fetcherspending = 0
    self.fetcherjobsrunning = 0
    self.fetcherjobspending = 0
    self.fetcherthreads = []
    self.uploadqueue = Queue()
    self.uploaderthreads = []
    self.lastidentifier = None
    self.jobepoch = 0
    self.lpepoch = 0
    
    
  def _start(self):
    super(BCJSONRPCWorkSource, self)._start()
    self.host = self.settings.host
    self.port = self.settings.port
    self.getworkconnections = self.settings.getworkconnections
    self.uploadconnections = self.settings.uploadconnections
    self.longpollconnections = self.settings.longpollconnections
    if not self.settings.host or not self.settings.port: return
    self.shutdown = False
    for i in range(self.getworkconnections):
      thread = Thread(None, self.fetcher, "%s_fetcher_%d" % (self.settings.name, i))
      thread.daemon = True
      thread.start()
      self.fetcherthreads.append(thread)
    for i in range(self.uploadconnections):
      thread = Thread(None, self.uploader, "%s_uploader_%d" % (self.settings.name, i))
      thread.daemon = True
      thread.start()
      self.uploaderthreads.append(thread)
    
    
  def _stop(self):
    self.runcycle += 1
    self.shutdown = True
    with self.fetcherlock: self.fetcherlock.notify_all()
    for thread in self.fetcherthreads: thread.join(1)
    for i in self.uploaderthreads: self.uploadqueue.put(None)
    for thread in self.uploaderthreads: thread.join(1)
    super(BCJSONRPCWorkSource, self)._stop()
    
    
  def _get_statistics(self, stats, childstats):
    super(BCJSONRPCWorkSource, self)._get_statistics(stats, childstats)
    stats.supports_rollntime = self.stats.supports_rollntime
    
  
  def _get_running_fetcher_count(self):
    return self.fetchersrunning, self.fetcherjobsrunning + self.fetcherjobspending
  
  
  def _start_fetcher(self):
    count = len(self.fetcherthreads)
    if not count: return False
    with self.fetcherlock:
      if self.fetchersrunning >= count: return 0, 0
      self.fetcherjobspending += self.estimated_jobs
      self.fetchersrunning += 1
      self.fetcherspending += 1
      self.fetcherlock.notify()
    return 1, self.estimated_jobs


  def fetcher(self):
    conn = None
    while not self.shutdown:
      with self.fetcherlock:
        while not self.fetcherspending:
          self.fetcherlock.wait()
          if self.shutdown: return
        self.fetcherspending -= 1
        myjobs = self.estimated_jobs
        self.fetcherjobsrunning += myjobs
        self.fetcherjobspending -= myjobs
        if not self.fetcherspending or self.fetcherjobspending < 0: self.fetcherjobspending = 0
      jobs = None
      try:
        req = json.dumps({"method": "getwork", "params": [], "id": 0}).encode("utf_8")
        headers = {"User-Agent": self.useragent, "X-Mining-Extensions": self.extensions,
                   "Content-Type": "application/json", "Content-Length": len(req), "Connection": "Keep-Alive"}
        if self.auth != None: headers["Authorization"] = self.auth
        try:
          if conn:
            try:
              epoch = self.jobepoch
              now = time.time()
              conn.request("POST", self.settings.path, req, headers)
              conn.sock.settimeout(self.settings.getworktimeout)
              response = conn.getresponse()
            except:
              conn = None
              self.core.log(self, "Keep-alive job fetching connection died\n", 500)
          if not conn:
            conn = http_client.HTTPConnection(self.settings.host, self.settings.port, True, self.settings.getworktimeout)
            epoch = self.jobepoch
            now = time.time()
            conn.request("POST", self.settings.path, req, headers)
            conn.sock.settimeout(self.settings.getworktimeout)
            response = conn.getresponse()
          data = response.read()
        except:
          conn = None
          raise
        with self.statelock:
          if not self.settings.longpollconnections: self.signals_new_block = False
          else:
            lpfound = False
            headers = response.getheaders()
            for h in headers:
              if h[0].lower() == "x-long-polling":
                lpfound = True
                url = h[1]
                if url == self.longpollurl: break
                self.longpollurl = url
                try:
                  if url[0] == "/": url = "http://" + self.settings.host + ":" + str(self.settings.port) + url
                  if url[:7] != "http://": raise Exception("Long poll URL isn't HTTP!")
                  parts = url[7:].split("/", 1)
                  if len(parts) == 2: path = "/" + parts[1]
                  else: path = "/"
                  parts = parts[0].split(":")
                  if len(parts) != 2: raise Exception("Long poll URL contains host but no port!")
                  host = parts[0]
                  port = int(parts[1])
                  self.core.log(self, "Found long polling URL: %s\n" % (url), 500, "g")
                  self.signals_new_block = True
                  self.runcycle += 1
                  for i in range(self.settings.longpollconnections):
                    thread = Thread(None, self._longpollingworker, "%s_longpolling_%d" % (self.settings.name, i), (host, port, path))
                    thread.daemon = True
                    thread.start()
                except Exception as e:
                  self.core.log(self, "Invalid long polling URL: %s (%s)\n" % (url, str(e)), 200, "y")
                break
            if self.signals_new_block and not lpfound:
              self.runcycle += 1
              self.signals_new_block = False
        jobs = self._build_jobs(response, data, epoch, now, "getwork")
      except:
        self.core.log(self, "Error while fetching job: %s\n" % (traceback.format_exc()), 200, "y")
        self._handle_error()
      finally:
        with self.fetcherlock:
          self.fetchersrunning -= 1
          self.fetcherjobsrunning -= myjobs
      if jobs:
        self._push_jobs(jobs, "getwork response")
        
        
  def nonce_found(self, job, data, nonce, noncediff):
    self.uploadqueue.put((job, data, nonce, noncediff))
      
      
  def uploader(self):
    conn = None
    while not self.shutdown:
      share = self.uploadqueue.get()
      if not share: continue
      job, data, nonce, noncediff = share
      tries = 0
      while True:
        try:
          req = json.dumps({"method": "getwork", "params": [hexlify(data).decode("ascii")], "id": 0}).encode("utf_8")
          headers = {"User-Agent": self.useragent, "X-Mining-Extensions": self.extensions,
                     "Content-Type": "application/json", "Content-Length": len(req)}
          if self.auth != None: headers["Authorization"] = self.auth
          try:
            if conn:
              try:
                conn.request("POST", self.settings.path, req, headers)
                response = conn.getresponse()
              except:
                conn = None
                self.core.log(self, "Keep-alive share upload connection died\n", 500)
            if not conn:
              conn = http_client.HTTPConnection(self.settings.host, self.settings.port, True, self.settings.sendsharetimeout)
              conn.request("POST", self.settings.path, req, headers)
              response = conn.getresponse()
            rdata = response.read()
          except:
            conn = None
            raise
          rdata = json.loads(rdata.decode("utf_8"))
          result = False
          if rdata["result"] == True: result = True
          elif rdata["error"] != None: result =  rdata["error"]
          else:
            headers = response.getheaders()
            for h in headers:
              if h[0].lower() == "x-reject-reason":
                result = h[1]
                break
          if result is not True:
            self.jobepoch += 1
            self._cancel_jobs(True)
          self._handle_success()
          job.nonce_handled_callback(nonce, noncediff, result)
          break
        except:
          self.core.log(self, "Error while sending share %s (difficulty %.5f): %s\n" % (hexlify(nonce).decode("ascii"), noncediff, traceback.format_exc()), 200, "y")
          tries += 1
          self._handle_error(True)
          time.sleep(min(30, tries))


  def _longpollingworker(self, host, port, path):
    runcycle = self.runcycle
    tries = 0
    starttime = time.time()
    conn = None
    while True:
      if self.runcycle > runcycle: return
      try:
        headers = {"User-Agent": self.useragent, "X-Mining-Extensions": self.extensions, "Connection": "Keep-Alive"}
        if self.auth != None: headers["Authorization"] = self.auth
        if conn:
          try:
            if conn.sock: conn.sock.settimeout(self.settings.longpolltimeout)
            epoch = self.lpepoch + 1
            conn.request("GET", path, None, headers)
            conn.sock.settimeout(self.settings.longpollresponsetimeout)
            response = conn.getresponse()
          except:
            conn = None
            self.core.log(self, "Keep-alive long poll connection died\n", 500)
        if not conn:
          conn = http_client.HTTPConnection(host, port, True, self.settings.longpolltimeout)
          epoch = self.lpepoch + 1
          conn.request("GET", path, None, headers)
          conn.sock.settimeout(self.settings.longpollresponsetimeout)
          response = conn.getresponse()
        if self.runcycle > runcycle: return
        if epoch > self.lpepoch:
          self.lpepoch = epoch
          self.jobepoch += 1
          self._cancel_jobs(True)
        data = response.read()
        jobs = self._build_jobs(response, data, self.jobepoch, time.time() - 1, "long poll", True, True)
        if not jobs: continue
        self._push_jobs(jobs, "long poll response")
      except:
        conn = None
        self.core.log(self, "Long poll failed: %s\n" % (traceback.format_exc()), 200, "y")
        tries += 1
        if time.time() - starttime >= 60: tries = 0
        if tries > 5: time.sleep(30)
        else: time.sleep(1)
        starttime = time.time()
        
        
  def _build_jobs(self, response, data, epoch, now, source, ignoreempty = False, discardiffull = False):
    decoded = data.decode("utf_8")
    if len(decoded) == 0 and ignoreempty:
      self.core.log(self, "Got empty %s response\n" % source, 500)
      return
    decoded = json.loads(decoded)
    data = unhexlify(decoded["result"]["data"].encode("ascii"))
    target = unhexlify(decoded["result"]["target"].encode("ascii"))
    try: identifier = int(decoded["result"]["identifier"])
    except: identifier = None
    if identifier != self.lastidentifier:
      self._cancel_jobs()
      self.lastidentifier = identifier
    self.blockchain.check_job(Job(self.core, self, 0, data, target, True, identifier))
    roll_ntime = 1
    expiry = 60
    isp2pool = False
    headers = response.getheaders()
    for h in headers:
      if h[0].lower() == "x-is-p2pool" and h[1].lower() == "true": isp2pool = True
      elif h[0].lower() == "x-roll-ntime" and h[1] and h[1].lower() != "n":
        roll_ntime = 60
        parts = h[1].split("=", 1)
        if parts[0].strip().lower() == "expire":
          try: roll_ntime = int(parts[1])
          except: pass
        expiry = roll_ntime
    if isp2pool: expiry = 60
    self.stats.supports_rollntime = roll_ntime > 1
    if epoch != self.jobepoch:
      self.core.log(self, "Discarding %d jobs from %s response because request was issued before flush\n" % (roll_ntime, source), 500)
      with self.stats.lock: self.stats.jobsreceived += roll_ntime
      return
    if self.core.workqueue.count > self.core.workqueue.target * (1 if discardiffull else 5):
      self.core.log(self, "Discarding %d jobs from %s response because work buffer is full\n" % (roll_ntime, source), 500)
      with self.stats.lock: self.stats.jobsreceived += roll_ntime
      return
    expiry += now - self.settings.expirymargin
    midstate = Job.calculate_midstate(data)
    prefix = data[:68]
    timebase = struct.unpack(">I", data[68:72])[0]
    suffix = data[72:]
    return [Job(self.core, self, expiry, prefix + struct.pack(">I", timebase + i) + suffix, target, midstate, identifier) for i in range(roll_ntime)]
  