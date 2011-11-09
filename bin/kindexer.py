#!/usr/bin/python
#
# nfdump-tools - Inspecting the output of nfdump
#
# Copyright (C) 2011 CIRCL Computer Incident Response Center Luxembourg (smile gie)
# Copyright (C) 2011 Gerard Wagener
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.


import ConfigParser
import sys
import os
import redis
import time
import kindcommon


class Kindexer(object):

    def __init__(self, configfile):
        self.load_config (configfile)
        self.rd = redis.Redis()
        self.kco = kindcommon.KindCommon(self.config)

    def load_config(self,configfile):
        try:
            self.config = ConfigParser.ConfigParser()
            self.config.readfp(open(configfile))
            p = self.config.get("indexer","dbdir")
            #Check if mandatory directories exist
            if os.path.exists(p) == False:
                raise IOError("dbdir="+ p +" does not exists ")
            if os.path.exists(self.config.get("indexer", "tmpdir")) == False:
                raise IOError("tmpfs mount point does not exists")
            self.config.get('indexer','prgpidfile')
            p = self.config.get('indexer','prg')
            if (os.path.exists(p) == False):
                raise IOError("Program does not exists "+p)
            if (os.access(p, os.X_OK) == False):
                raise IOError("Program is not executable")
            self.config.get("indexer","name")
            self.config.get("indexer", "prg")

        except ConfigParser.NoOptionError,e:
            sys.stderr.write("Config Error: "+str(e) + '\n')
            sys.exit(1)
        except ValueError,v:
            sys.stderr.write("Config Error: "+str(v) + '\n')
            sys.exit(1)
        except IOError,w:
            sys.stderr.write("Could not load config file "+ str(w)+"\n")
            sys.exit(1)

    #Go through all netflow directories and check in redis if the file name
    #has already be indexed. If no add a key k:filename with a status value
    #queued, run, done
    def sync_filenames(self):
        #Go through all the directories
        dirs = self.kco.read_flow_dirs()
        if len(dirs) == 0:
            self.kco.dbg("No directories to process")
            return
        for dir in dirs:
            self.kco.dbg("Going through directory "+ dir)
            for fl in os.listdir(dir):
                #Check if netflow file has already be indexed
                fdb = self.kco.get_databasefile(fl)
                if os.path.exists(fdb):
                    self.kco.dbg("The database " + fdb+ " was found for the file " + fl +  " skip" )
                    continue
                k = "k:"+ fl
                if self.rd.get(k) == None:
                    #The file was not seen before
                    self.rd.set(k,"QUEUED") # Majorly informative
                    qname=self.kco.get_queue_name(fl)
                    self.kco.dbg("Adding " + str(fl) +  "to queue " + qname)
                    #Create an individual queue for an indexer
                    self.rd.rpush(qname, dir+os.sep+fl)

    def check_pid_file(self):
        if os.path.exists(self.config.get('indexer','prgpidfile')) == True:
            sys.stderr.write('Old instance of kindexer was found, abort\n')
            sys.exit(1)

    def cexec(self,cmd):
        self.kco.dbg(""+time.strftime("%Y-%m-%d %H:%M:%S") + \
                   " Executing "+ cmd)
        starttime=time.time()
        ret = os.system(cmd)
        endtime=time.time()
        d=endtime - starttime
        self.kco.dbg( "Processing time "+ str(d) + " seconds")
        if ret != 0:
            sys.stderr.write('cmd failed, abort, exit code ' +str(ret)+'\n')
            exit (1)


    def do_the_job(self, nffile):
        #Check if there are remaining parts of an old instance
        self.check_pid_file()
        tf=self.config.get("indexer","tmpdir") + os.sep + 'current.kch'
        cmd = self.config.get('indexer','prg')+" -r "+ nffile + " -p "+\
        self.config.get('indexer','prgpidfile') +" -d "+  tf

        self.cexec(cmd)

    def copy_database(self, nffile):
        target_db = self.kco.get_databasefile(nffile)
        target_tempdb = self.kco.get_temp_databasefile(nffile)
        tf=self.config.get("indexer","tmpdir") + os.sep + 'current.kch'
        cmd="mv " + tf  + " "+ target_tempdb
        #Check if there is already a database
        if (os.path.exists(target_db)):
            sys.stderr.write("Target dabase already exists, abort "+target_db \
            + "\n")
            sys.exit(1)
        #This command takes time and the transfer should not be visible to
        #other processes. Therefore, the file is copied in a hidden file.
        #When the transfer is done the file is removed which is assumed to be
        #atomic
        self.cexec(cmd)
        self.kco.dbg('Renaming '+ target_tempdb + ' -> ' + target_db )
        try:
            os.rename(target_tempdb, target_db)
        except OSError,e:
            self.kco.dbg("Could not rename file "+ str(e))
            sys,exit(1)
    def check_current_database(self):
        p = self.config.get('indexer','tmpdir') + os.sep + 'current.kch'
        if os.path.exists(p):
            sys.stderr.write('It seemed that the indexer daemon had been aborted current.kch is still there, please remove\n')
            sys.exit(1)

    def process_queue(self, qname):
        nffile = "a"
        nbritems = self.rd.llen(qname)
        if (nbritems != 288):
            print "WARNING!! Incomplete day of netflow nbritems=", nbritems
        previousnffile=None
        cnt=0
        while nffile != None:
            nffile = self.rd.lpop(qname)
            if nffile != None:
                cnt = cnt+1
                self.kco.dbg("[" + str(cnt) + "/" +  str(nbritems) + \
                             "] Selected queue "+ qname)
                self.do_the_job(nffile)
                previousnffile = nffile
                #After the processing update informative queue
                self.rd.set("k:"+os.path.basename(nffile),"DONE")
        #Returns the last processed file in order to identify the database location
        return previousnffile

    def process(self):
        self.check_current_database()
        for qname in self.rd.keys("dq:*"):
            self.kco.dbg("Processing queue "+qname)
            lastfile = self.process_queue( qname)
            #The entire queue corresponding to a day of netflow has been indexed
            #Copy now the database from ramdisk to solid disk
            self.copy_database(lastfile)
            #Remove the attached queue
            self.kco.dbg("Removing queue " + qname)
            self.rd.delete(qname)

if __name__ == '__main__':
    try:
        ki = Kindexer('kindexer.cfg')
        ki.sync_filenames()
        ki.process()
        ki.kco.dbg("Kindexer bot stopped")
        sys.exit(0)
    except redis.exceptions.ConnectionError,c:
        sys.stderr.write(str(c)+'\n')
        sys.exit(1)
