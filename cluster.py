#!/usr/bin/env python
import curses, curses.textpad, curses.ascii
import os
import subprocess, sys, getopt, time,shlex
from subprocess import Popen
import thread
import threading
import cluster_storage
from collections import defaultdict

from SimpleXMLRPCServer import SimpleXMLRPCServer
import random
import psutil
import numpy
from IPython.kernel import client

WHITE=0
RED=1
GREEN=2
YELLOW=3
MAGENTA=4
BLUE=5
CYAN=6

TIMEOUT=480

#current screen info
class gb:
    scr = None
    lock = None

    wlog = None
    wlog_currow = 0
    wlog_size = (0,0)

    wcom = None
    wcom_status = None
    
    wstatus = None
    wstatus_progress = None

class status:
    envname = None
    address = None
    port = None
    iport = None

    lock = None
    controller = False
    controller_monitor = None
    taskclient = None
    queue_status = defaultdict(int)
    controller_cpu = 0.0
    
    local_engines = []

    pbs_engines = []
    pbs_stats = (0,0,0)

    grid_engines = []
    grid_engines_count = []
    grid_status = dict()
    grid_count_status = defaultdict(int)
    codeid = ""
    
    engine_type_count = defaultdict(int)
    engine_type_stats = dict()
NOSTAT = (0.0, 0.0, 0.0, 0.0, 0.0)

#create a bordered window
def border_win(parent,height,width,x,y,**kwargs):
    if(not 'notop' in kwargs):
        parent.hline(x,y,'.',width)
        height -= 1
        x += 1
    if(not 'nobottom' in kwargs):
        parent.hline(x+height-1,y,'.',width)
        height -= 1
    if(not 'noleft' in kwargs):
        parent.vline(x,y,'.',height)
        width -= 1
        y += 1
    if(not 'noright' in kwargs):
        parent.vline(x,y+width-1,'.',height)
        width -= 1
    w = parent.subwin(height, width,x,y)
    w.noutrefresh()
    parent.refresh()
    return w


#threaded output of a file to the log window
def thread_log_output(file,gb,color,filter):
    while 1:
        line = file.readline()
        if(not line):
            break
         
        if(filter and len([f for f in filter if f in line]) > 0):
            pass
        else:    
            add_log_line(gb,line,color)

def log_output(file,gb,color=0,filter=[]):
    t = thread.start_new_thread(thread_log_output,(file,gb,color,filter))
    return t


def get_pbs_stats(status):
    if(not status.pbs_engines):
        return (0,0,0)
    status.lock.acquire()
    try:
        cmd = "qstat " + " ".join(status.pbs_engines)
        args = shlex.split(cmd)
        p = Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        cmd = "gawk '{print $5}'"
        args = shlex.split(cmd)
        p2 = Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,stdin=p.stdout)
        output = p2.communicate()[0]
        z = defaultdict(int)
        for k in output[2:]:
            z[k] += 1
        res = (len(status.pbs_engines),z['Q'],z['R'])
    finally:
        status.lock.release()
    return res

def get_stat_str(stats):
    cpu,memp,mempmax,mem,memmax = stats
    if(cpu < 0.50 or mempmax > 0.90):
        color = RED
    elif(cpu < 0.80 or mempmax > 0.70):
        color = YELLOW
    else:
        color = GREEN

    descr = "(" +  '{:.2%}'.format(cpu) + " CPU, " + \
            '{:.2%}'.format(memp) + "/" + '{:.2%}'.format(mempmax)  + "/" + \
            '{}'.format(int(mem)) + "MB MEM)"
    return descr,color

#thread display status
def thread_display_status(gb,status):
    while not status.stop_status_display:
        status.lock.acquire()
        gb.lock.acquire()
        try:
            if(gb.wcom_status and gb.wcom_status.getmaxyx()[1] > 50):
                sizey = gb.wcom_status.getmaxyx()[1]
                gb.wcom_status.erase()
                gb.wcom_status.addstr(1,1,"Controller: ")
                if(controller_online(status)):
                    r = status.controller_cpu / 100.0
                    if(r > 0.80):
                        color = RED
                    elif(r > 0.50):
                        color = YELLOW
                    else:
                        color = GREEN
                    gb.wcom_status.addstr(1,18,"ONLINE", curses.color_pair(color))
                    gb.wcom_status.addstr(1,52,"(" +  '{:.2%}'.format(r) + " CPU)",curses.color_pair(color))
                else:
                    gb.wcom_status.addstr(1,18,"OFFLINE",curses.color_pair(YELLOW))
                
                
                gb.wcom_status.addstr(3,1,"Local engines: ")
                unaccounted = len(status.local_engines) - status.engine_type_count['LOCAL']
                if(status.local_engines and not unaccounted != 0):
                    color = GREEN
                else:
                    color = YELLOW
                if(unaccounted != 0):
                    gb.wcom_status.addstr(3,18,str(len(status.local_engines)) + "("+str(-unaccounted) +")",curses.color_pair(color))
                else:
                    gb.wcom_status.addstr(3,18,str(len(status.local_engines)),curses.color_pair(color))

                if(sizey > 90 and status.local_engines):
                    stats = status.engine_type_stats.get("LOCAL",NOSTAT)
                    descr,color = get_stat_str(stats)
                    gb.wcom_status.addstr(3,52,descr,curses.color_pair(color))
                    

                gb.wcom_status.addstr(5,1,"PBS engines: ")
                stats = get_pbs_stats(status)
                status.pbs_stats = stats
                unaccounted = stats[2] - status.engine_type_count['PBS']
                if(stats[2] and not unaccounted != 0):
                    color = GREEN
                else:
                    color = YELLOW
                if(unaccounted != 0):
                    gb.wcom_status.addstr(3,18,str(stats[2]) + "("+str(-unaccounted) +")",curses.color_pair(color))
                else:
                    gb.wcom_status.addstr(5,18,str(stats[2]),curses.color_pair(color))

                if(stats[1]):
                    gb.wcom_status.addstr(5,28,'Queued: ' + str(stats[1]),curses.color_pair(YELLOW))
                failed = stats[0] - (stats[1] + stats[2])
                if(failed):
                    gb.wcom_status.addstr(5,40,'Failed: ' + str(failed),curses.color_pair(RED))

                if(sizey > 90 and status.pbs_engines):
                    stats = status.engine_type_stats.get("PBS",NOSTAT)
                    descr,color = get_stat_str(stats)
                    gb.wcom_status.addstr(5,52,descr,curses.color_pair(color))

                gb.wcom_status.addstr(7,1,"GRID engines: ")
                running = status.grid_count_status['running']
                queued = status.grid_count_status['scheduled'] + status.grid_count_status['submitted'] + \
                         status.grid_count_status['waiting'] + status.grid_count_status['ready'] 
                failed = status.grid_count_status['done (success)'] + status.grid_count_status['cleared'] + \
                         status.grid_count_status['aborted']
                unaccounted = running - status.engine_type_count['GRID']
                if(running and not unaccounted != 0):
                    color = GREEN
                else:
                    color = YELLOW
                if(unaccounted != 0):
                    gb.wcom_status.addstr(7,18,str(running) + "("+str(-unaccounted) +")",curses.color_pair(color))
                else:
                    gb.wcom_status.addstr(7,18,str(running),curses.color_pair(color))
                if(queued):
                    gb.wcom_status.addstr(7,28,'Queued: ' + str(queued),curses.color_pair(YELLOW))
                if(failed):
                    gb.wcom_status.addstr(7,40,'Failed: ' + str(failed),curses.color_pair(RED))
                
                if(sizey > 90 and status.grid_engines):
                    stats = status.engine_type_stats.get("GRID",NOSTAT)
                    descr,color = get_stat_str(stats)
                    gb.wcom_status.addstr(7,52,descr,curses.color_pair(color))
               
                
                if(status.queue_status):
                    descr = "Failed: " + str(status.queue_status.get("failed",0))
                    descr += "  Pending: " + str(status.queue_status.get("pending",0))
                    descr += "  Scheduled: " + str(status.queue_status.get("scheduled",0))
                    descr += "  Ready: " + str(status.queue_status.get("succeeded",0))
                    gb.wcom_status.addstr(9,1,"Queue:")
                    gb.wcom_status.addstr(9,18,descr)
                
                gb.wcom_status.refresh()
        except Exception,e:
            add_log_line(gb,"Exception in display thread: " + str(e),RED)
        finally:
            gb.lock.release()
            status.lock.release()
        time.sleep(1)

def start_status_display(gb,status):
    status.stop_status_display = False
    t = threading.Thread(target=thread_display_status,args=(gb,status))
    t.daemon=True
    t.start()
    return t

def stop_status_display(status,t):
    set_status_message(gb,"Waiting for status thread to terminate..",YELLOW)
    status.stop_status_display = True
    t.join()

def controller_online(status):
    return (status.controller and status.controller.poll() is None)
    

#initialize windows/lock, etc.
def build_windows(gb):
    if(not gb.lock): 
        gb.lock = threading.RLock()
    gb.lock.acquire()
    try:

        gb.scr.erase()
        curses.start_color()
        curses.curs_set(0)
        size = gb.scr.getmaxyx()
        com_win_rows = max(0,min(20,size[0]-2))
        status_win_rows = max(0,min(2,size[0] - com_win_rows))
        log_win_rows = max(0,size[0] - com_win_rows - status_win_rows)


        #init colors
        curses.init_pair(RED,curses.COLOR_RED,curses.COLOR_BLACK)
        curses.init_pair(GREEN,curses.COLOR_GREEN,curses.COLOR_BLACK)
        curses.init_pair(YELLOW,curses.COLOR_YELLOW,curses.COLOR_BLACK)
        curses.init_pair(MAGENTA,curses.COLOR_MAGENTA,curses.COLOR_BLACK)
        curses.init_pair(BLUE,curses.COLOR_BLUE,curses.COLOR_BLACK)
        curses.init_pair(CYAN,curses.COLOR_CYAN,curses.COLOR_BLACK)
        
        #log window
        if(log_win_rows > 2):
            gb.wlog = border_win(gb.scr,log_win_rows,size[1],0,0)
            gb.wlog_size = gb.wlog.getmaxyx()
            gb.wlog_currow = 0
            gb.wlog.setscrreg(0,gb.wlog_size[0]-1)
            gb.wlog.idlok(True)
            gb.wlog.scrollok(True)
            gb.wlog.noutrefresh()
        else:
            gb.wlog = None

        #com window
        if(com_win_rows > 2):
            com_win_cols = size[1] / 2
            if(com_win_cols > 55):
                comlog_win_cols = size[1] - 55
                com_win_cols = 55
                gb.wcom_status = border_win(gb.scr,com_win_rows,comlog_win_cols,log_win_rows,com_win_cols,notop=True,noleft=True)
                gb.wcom_status.noutrefresh()
            else:
                com_win_cols = size[1]
                gb.wcom_status = None
        
            gb.wcom = border_win(gb.scr,com_win_rows,com_win_cols,log_win_rows,0,notop=True)
            gb.wcom.noutrefresh()
        else:
            gb.wcom = None
            gb.wcom_status = None
        
        #status window
        if(status_win_rows == 2):
            gb.wstatus = gb.scr.subwin(status_win_rows,size[1],log_win_rows + com_win_rows,0)
            gb.wstatus.noutrefresh()
        else:
            gb.wstatus = None

        init_commands(gb)
        gb.scr.refresh()
    finally:
        gb.lock.release()    

#add line to log windows
def add_log_line(gb,line,color=0):
    gb.lock.acquire()
    try:
        if(line and line[-1] == '\n'):
            gb.log_file.write(line)
        else:
            gb.log_file.write(line + '\n')
        if(gb.wlog):
            try:
                line = str(line.decode('ascii'))
            except UnicodeDecodeError:
                line = "Invalid line format (unicode) encountered"
                color = RED
            line = line[:(gb.wlog_size[1] - 2)]
            if(line and line[-1] == '\n'): line = line[:-1]
            if(gb.wlog_currow < gb.wlog_size[0]):
                gb.wlog.addstr(gb.wlog_currow,1,line,curses.color_pair(color))
                gb.wlog_currow += 1
            else:
                gb.wlog.scroll()
                gb.wlog.addstr(gb.wlog_size[0] - 1,1,line,curses.color_pair(color))
            gb.wlog.refresh()
    finally:
        gb.lock.release()

#set status message iin status window
def set_status_message(gb,line,color=0):
    gb.lock.acquire()
    try:
        if(gb.wstatus):
            gb.wstatus.addstr(0,0,line,curses.color_pair(color))
            gb.wstatus.clrtoeol()
            gb.wstatus.refresh()
    finally:
        gb.lock.release()

def set_progress_bar(gb,s):
    gb.lock.acquire()
    try:
        if(gb.wstatus):
            if(s < 0.0 or s > 1.0):
                add_log_line(gb, "Progress bar value out of range: " + str(s),RED)
            else:
                size = gb.wstatus.getmaxyx()
                total_i = size[1] - 3
                full_i = int(s * total_i)
                rem_i = total_i - full_i
                line = "[" + ('#' * full_i) + (' ' * rem_i) + "]"
                gb.wstatus.addstr(1,0,line,curses.color_pair(MAGENTA))
                gb.wstatus.clrtoeol()
                gb.wstatus.refresh()
    except Exception,e:
        add_log_line(gb,"Exception in set_progress_bar: " + str(e),RED)
    finally:
        gb.lock.release()

def stop_progress_bar(gb):
    gb.lock.acquire()
    try:
        if(gb.wstatus):
            gb.wstatus.addstr(1,0,"")
            gb.wstatus.clrtoeol()
            gb.wstatus.refresh()
    finally:
        gb.lock.release()

#print commands in command windows
def init_commands(gb):
    gb.lock.acquire()
    try:
        gb.wcom.addstr(1,1,"Start/stop controller: a/z")
        gb.wcom.addstr(2,1,"Start/stop local engines: s/x [n]")
        gb.wcom.addstr(3,1,"Start/stop PBS engines: d/c [n]")
        gb.wcom.addstr(4,1,"Start/stop GRID engines: f/v [n]")
        gb.wcom.addstr(5,1,"Reload all engines: w")
        gb.wcom.addstr(6,1,"Restart all engines: e")
        gb.wcom.addstr(8,1,"Refresh screen: r")
        gb.wcom.addstr(9,1,"Shutdown: q")
        gb.wcom.refresh()
    finally:
        gb.lock.release()

#enter command in command window
def enter_command(gb,argument="Command: "):
    gb.lock.acquire()
    try:
        command = ""
        size = gb.wcom.getmaxyx()
        x = size[0] - 1
        gb.wcom.addstr(x,1,argument)
        gb.wcom.clrtoeol()
        gb.wcom.refresh()
        while 1:
            gb.lock.release()
            c = gb.scr.getch()
            gb.lock.acquire()
            if c == curses.ascii.LF:
                break
            elif c == curses.ascii.DEL:
                command = command[:-1]
            else:
                if(c > 256):
                    set_status_message(gb,"Keycode not supported: " + str(c))
                else:
                    command += chr(c)
            gb.wcom.addstr(x,1,argument + command[:(size[1] - len(argument) - 2)])
            gb.wcom.clrtoeol()
            gb.wcom.refresh()

        gb.wcom.addstr(x,1,"")
        gb.wcom.clrtoeol()
        gb.wcom.refresh()
    finally:
        gb.lock.release()
    return command

def get_number(gb,argument,default=0):
    cmd = enter_command(gb,argument)
    if(not cmd):
        n = default
    else:
        try:
            n = int(cmd)
        except ValueError:
            set_status_message(gb,"Number of engines should be a number",YELLOW)
            n = None
    return n

#start ipython cluster controller
def start_controller(gb,status):
    set_status_message(gb,"Waiting for controller to finish initialization...",YELLOW)
    set_progress_bar(gb,0.0)
    status.lock.acquire()
    try:
        cwd = os.getcwd()
        furl_path = cwd + "/engine.furl"
        cmd = "ipcontroller --engine-port=" + str(status.iport) + " --engine-location=" + status.address + " --engine-furl=" + furl_path
        add_log_line(gb,"Executing: " + cmd,GREEN)
        args = shlex.split(cmd)
        pc = Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        status.controller = pc
        status.controller_monitor = psutil.Process(pc.pid)
    finally:
        status.lock.release()
    log_output(pc.stdout,gb,WHITE,['distributing Tasks','Getting task result','Queuing task','sync properties'])
    log_output(pc.stderr,gb,RED)
    set_progress_bar(gb,0.4)

    while(pc.poll() is None and not os.path.exists(furl_path)):
        time.sleep(1)

    if(not pc.poll() is None):
        set_status_message(gb,"Controller startup failed!",RED)
    else:
        retry = 3
        set_status_message(gb,"Starting controller client...",YELLOW)
        time.sleep(5)
        while(retry):
            try:
                status.taskclient = client.TaskClient()
                retry = 0
            except Exception, e:
                retry -=1
                if(retry == 0):
                    raise e
                else:
                    time.sleep(15)
        stop_progress_bar(gb)
        set_status_message(gb,"Controller online",GREEN)
        add_log_line(gb,"Controller brought online",MAGENTA)

#stop ipython cluster controller
def stop_controller(gb,status):
    status.lock.acquire()
    try:
        if(status.local_engines):
            stop_locals(gb,status,len(status.local_engines))
        if(status.pbs_engines):
            stop_pbs(gb,status,len(status.pbs_engines))
        if(status.grid_engines):
            stop_grid(gb,status,sum(status.grid_engines_count))
        
        status.taskclient = None
        set_status_message(gb,"Waiting for controller to terminate..",YELLOW)
        if(controller_online(status)):
            status.controller.terminate()
            status.controller = None
            status.controller_monitor = None
            time.sleep(2) #finishes log output, lets threads terminate
    finally:
        status.lock.release()
    set_status_message(gb,"Controller offline..",YELLOW)
    add_log_line(gb,"Controller stopped",MAGENTA)


def start_locals(gb,status,n):
    status.lock.acquire()
    try:
        if(not controller_online(status)):
            set_status_message(gb,"Controller is not online",YELLOW)
        elif(n < 0):
            set_status_message(gb,"Invalid number of engines to be started: " + str(n),YELLOW)
        else: 
            set_status_message(gb,"Starting local engines",YELLOW)
            chief_path = os.path.dirname(os.path.realpath(__file__)) + "/ipengine_chief.py"
            cmd = chief_path + " -t LOCAL -a " + str(status.address) + " -p " + str(status.port)
            add_log_line(gb,"Starting engines using: " + cmd,GREEN)
            args = shlex.split(cmd)
        
            set_progress_bar(gb,0.0)
            for i in range(n):
                e = Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                log_output(e.stderr,gb,RED)
                status.local_engines.append(e)
                time.sleep(1)
                set_progress_bar(gb,float(i+1) / float(n))
            time.sleep(2)
            stop_progress_bar(gb)
            set_status_message(gb,"Local engines started",GREEN)
            add_log_line(gb,str(n) + " local engines started",MAGENTA)
    finally:
        status.lock.release()

def stop_locals(gb,status,n):
    status.lock.acquire()
    try:
        if(not status.local_engines and n != 0):
            set_status_message(gb,"No local engines running",YELLOW)
        elif(n < 0 or n > len(status.local_engines)):
            set_status_message(gb,"Invalid number of engines to be stopped: " + str(n),YELLOW)
        else:
            set_status_message(gb,"Stopping local engines",YELLOW)
            stop_engines = status.local_engines[:n]
            status.local_engines = status.local_engines[n:]
            for e in stop_engines:
                if(e.poll() is None):
                    e.terminate()
            
            time.sleep(3)
            set_status_message(gb,"Local engines stopped",GREEN)
            add_log_line(gb,str(n) + " local engines stopped",MAGENTA)
    finally: 
        status.lock.release()

def start_pbs(gb,status,n):
    status.lock.acquire()
    try:
        if(not controller_online(status)):
            set_status_message(gb,"Controller is not online",YELLOW)
        elif(n < 0):
            set_status_message(gb,"Invalid number of engines to be started: " + str(n),YELLOW)
        else: 
            set_status_message(gb,"Starting PBS engines",YELLOW)
            cwd = os.getcwd()
            furl_path = cwd + "/engine.furl"

            engine_path = os.path.dirname(os.path.realpath(__file__))
            cmd = "qsub -N ipython_engine -k n -V -v EPATH=" + engine_path + ",ADDRESS=" + status.address + ",PORT=" + status.port + " " + engine_path + "/ipython_engine"
            add_log_line(gb,"Starting engines using: " + cmd,GREEN)
            args = shlex.split(cmd)
        
            for i in range(n):
                eid = Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE).communicate()[0]
                status.pbs_engines.append(eid)
            time.sleep(2)
            set_status_message(gb,"PBS engines queued",GREEN)
            add_log_line(gb,str(n) + " PBS engines queued",MAGENTA)
    finally:
        status.lock.release()

def stop_pbs(gb,status,n):
    status.lock.acquire()
    try:
        if(not status.pbs_engines and n != 0):
            set_status_message(gb,"No PBS engines running",YELLOW)
        elif(n < 0 or n > len(status.pbs_engines)):
            set_status_message(gb,"Invalid number of engines to be stopped: " + str(n),YELLOW)
        else:
            set_status_message(gb,"Stopping PBS engines",YELLOW)
            stop_engines = status.pbs_engines[-n:]
            status.pbs_engines = status.pbs_engines[:-n]
            set_progress_bar(gb,0.0)
            for i,eid in enumerate(stop_engines):
                cmd = "qdel " + eid
                args = shlex.split(cmd)
                p = Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                log_output(p.stdout,gb,WHITE)
                log_output(p.stderr,gb,RED)
                p.wait()
                set_progress_bar(gb,float(i+1)/float(n))
            
            time.sleep(2)
            stop_progress_bar(gb)
            set_status_message(gb,"PBS engines stopped",GREEN)
            add_log_line(gb,str(n) + " PBS engines stopped",MAGENTA)
    finally: 
        status.lock.release()

def start_grid(gb,status,n):
    status.lock.acquire()
    try:
        if(not controller_online(status)):
            set_status_message(gb,"Controller is not online",YELLOW)
        elif(not status.grid_engines and not upload_code(gb,status)):
            add_log_line(gb,"Grid engines startup failed",RED)
        else:
            tmp_jdl = 'cluster.jdl'
            if(os.path.isfile(tmp_jdl)):
                os.remove(tmp_jdl)
            
            engine_path = os.path.dirname(os.path.realpath(__file__))
            cmd = "sed -e s:XXNJOBXX:" + str(n) + ":g -e s:XXADDRESSXX:" + status.address + ":g -e s:XXPORTXX:" + str(status.port) + ":g -e s:XXENVPATHXX:" + status.envname + ":g -e s:XXAPPPATHXX:" + engine_path + ":g " + engine_path + "/load_env_ipengine.jdl"
            args = shlex.split(cmd)
            Popen(args,stdout=open(tmp_jdl,'w')).wait()
            
            cmd = "glite-wms-job-submit -d " + os.environ["USER"] + " --nomsg " + tmp_jdl

            args = shlex.split(cmd)
            out = Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,cwd=engine_path).communicate()
            if(out[1]):
                add_log_line(gb,"GRID job submission failed",RED)
                for line in out[1].split('\n'):
                    add_log_line(gb,line,RED)
            else:
                jobid = out[0].strip().strip('\n')
                status.grid_engines.append(jobid)
                status.grid_engines_count.append(n)
                add_log_line(gb,"GRID job for " + str(n) + " engines submitted: " + jobid,GREEN)
    finally:
        status.lock.release()

def stop_grid(gb,status,n):
    status.lock.acquire()
    try:
        if(not status.grid_engines and n != 0):
            set_status_message(gb,"No GRID engines running",YELLOW)
        elif(n < 0 or n > sum(status.grid_engines_count)):
            set_status_message(gb,"Invalid number of engines to be stopped: " + str(n),YELLOW)
        else:
            while(n > 0):
                complete_cancel = [pos for pos,c in enumerate(status.grid_engines_count) if c <= n]
                if(len(complete_cancel) > 0):
                    pos = complete_cancel[0]
                    id = status.grid_engines.pop(pos)
                    _cancel_grid([id])
                    n -= status.grid_engines_count.pop(pos)
                    add_log_line(gb,"GRID job " + id + " cancelled",GREEN)
                else:
                    pos = numpy.array(status.grid_engines_count).argmin()
                    id = status.grid_engines[pos]
                    ids = _get_subids_grid(id)
                    _cancel_grid(ids[:n])
                    status.grid_engines_count[pos] -= n
                    n = 0                   
                    add_log_line(gb,"GRID job " + id + " partially cancelled (" + str(status.grid_engines_count[pos]) + " engines left)",GREEN)

    finally:
        status.lock.release()

def _get_subids_grid(id):
    idstatus = dict()
    cmd = "/usr/bin/python /opt/glite/bin/glite-wms-job-status --noint -v 0 " + id
    args = shlex.split(cmd)
    env = os.environ.copy()
    del env['PYTHONPATH']
    r = Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env).communicate()
    if(r[1]):   
        add_log_line(gb,"Error while inquiring GRID status:",RED)
        for line in r[1].split('\n'):
            add_log_line(gb,line,RED)
    else:
        res = r[0].split('\n')
        skipfirst = True
        for line in res:
            if('Status' in line):
                field = line.split(': ')[1]
                if('info' in line):
                    id = field
                else:
                    state = field.strip()
                    if(skipfirst):
                        skipfirst = False
                    else:
                        idstatus[id] = state.lower()
    
    id_by_state = defaultdict(list)
    for id,state in idstatus.iteritems():
        id_by_state[state].append(id)

    res = id_by_state.pop('submitted',[]) + id_by_state.pop('waiting',[]) + id_by_state.pop('ready',[]) + \
    id_by_state.pop('scheduled',[]) + id_by_state.pop('running',[]) + id_by_state.pop('done (success',[])
    for values in id_by_state.itervalues():
        res += values
    return res

def _cancel_grid(ids):
    while ids:
        cancel_ids = ids[:10]
        ids = ids[10:]
        cmd = "glite-wms-job-cancel --noint " + " ".join(cancel_ids)
        args = shlex.split(cmd)
        r = Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE).communicate()
        #if(r[1]):
        #    add_log_line(gb,"Error while performing GRID cancel",RED)
        #    for line in r[1].split('\n'):
        #        add_log_line(gb,line,RED)
   
def upload_code(gb,status):
    cwd = os.getcwd()
    code_path = "/tmp/__" + status.envname + "_code__.tgz"
    exclude_path = cwd + "/exclude.dat"
    
    cmd = "tar -chzf " + code_path + " ./ "
    if(os.path.isfile(exclude_path)):
        cmd += " -X " + exclude_path
    add_log_line(gb,"Archiving code using: " + cmd,GREEN)
    args = shlex.split(cmd)
    
    set_status_message(gb,"Creating code archive",YELLOW)
    set_progress_bar(gb,0.0)
    p = Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    log_output(p.stdout,gb,WHITE)
    log_output(p.stderr,gb,RED)
    r = p.wait()
    set_progress_bar(gb,0.4)
    
    if(r or not os.path.isfile(code_path)):
        add_log_line(gb,"Archiving of work directory failed.",RED)
        stop_progress_bar(gb)
        return False

    set_status_message(gb,"Storing code archive on grid",YELLOW)
    codeid = cluster_storage.submit_file(code_path)

    status.lock.acquire()
    status.codeid = codeid
    status.lock.release()
   
    #grid_code_path = os.environ['LFC_HOME'] + "/" + status.envname + "_code.tgz"
    #cs = cluster_storage.ClusterStorage()
    #cs.store_file(code_path,grid_code_path)
    os.remove(code_path)
    #set_progress_bar(gb,0.7)
    #set_status_message(gb,"Replicating code archive across grid",YELLOW)
    #cs.replicate_all(grid_code_path)
    stop_progress_bar(gb)
    set_status_message(gb,"Code archive uploaded",GREEN)
    add_log_line(gb,"Code archive uploaded to grid as " + codeid,GREEN)
    return True



def restart_engines(gb,status):
    if(status.grid_engines):
        if(not upload_code(gb,status)):
            add_log_line(gb,"Code update failed",RED)
            return
        grid_count = sum(status.grid_engines_count)
        stop_grod(gb,status,grid_count)
    else:
        grid_count = 0

    if(status.pbs_engines):
        pbs_count = len(status.pbs_engines)
        stop_pbs(gb,status,pbs_count)
    else:
        pbs_count = 0

    if(status.local_engines):
        local_count = len(status.local_engines)
        stop_locals(gb,status,local_count)
    else:
        local_count = 0

    if(grid_count):
        start_grid(gb,status,grid_count)

    if(pbs_count):
        start_pbs(gb,status,pbs_count)

    if(local_count):
        start_locals(gb,status,local_count)
    
    add_log_line(gb,"All cluster engines restarted",MAGENTA)


def init_status(status,envname,address,port,iport):
    status.lock = threading.RLock()
    status.envname = envname
    status.address = address
    status.port = port
    status.iport = iport



NOOP=0
DIE=1
RESTART_ENGINE=2

class Commands:
    lock = None
    server = None

    active_engines = dict()
    engine_types = dict()
    engine_stats = dict()

    pending_command = dict()
    command_counter = 0
    command_total = 0
    command_message = ""
    

def reload_engines(gb,status):
    if(status.grid_engines and not upload_code(gb,status)):
        add_log_line(gb,"Code update failed",RED)
    else:
        Commands.lock.acquire()
        try:
            ccounter = 0
            for engineid in Commands.active_engines.iterkeys():
                Commands.pending_command[engineid] = RESTART_ENGINE
                ccounter +=1
            if(ccounter == 0):
                set_status_message(gb, "No engines to reload...",YELLOW)
            else:
                Commands.command_total = ccounter
                set_status_message(gb, "Reloading " + str(ccounter) + " engines...",YELLOW)
                Commands.command_message = "Engine reloading of " + str(ccounter) + " engines is complete"
        finally:
            Commands.lock.release()


def register(myip,engine_type,cores,totmem):
    Commands.lock.acquire()
    try:
        myid = myip + ":0"
        counter = 0
        while myid in Commands.active_engines:
            counter += 1
            myid = myip + ":" + str(counter)
    
        Commands.active_engines[myid] = time.time()
        Commands.engine_types[myid] = engine_type
        Commands.engine_stats[myid] = (0.0,0.0,0.0,0.0)
        Commands.pending_command[myid] = NOOP
        add_log_line(gb,"Engine (" + engine_type + ", " + str(cores) + "C:" + "{:1.1f}".format((totmem / 1024.0 / float(cores))) + " GB) on " + myip + " registered as: " + myid,CYAN)
    finally:
        Commands.lock.release()
    if(engine_type == "GRID"):
        status.lock.acquire()
        try:
            mycodeid = status.codeid
        finally:
            status.lock.release()
    else:
        mycodeid = ""
    return myid,mycodeid

def poll(myid,cpu_usage,mem_usage,memphys,memvirt):
    Commands.lock.acquire()
    try:
        cmd = Commands.pending_command[myid]
        Commands.active_engines[myid] = time.time()
        Commands.engine_stats[myid] = (cpu_usage,mem_usage,memphys,memvirt)
        if(cmd): 
            Commands.pending_command[myid] = NOOP
            Commands.command_counter += 1
    except KeyError:
        add_log_line(gb,"Poll for unknown engine id: " + myid,RED)
        cmd = DIE
    finally:
        Commands.lock.release()
    return cmd
        

def unregister(myid):
    et = _unregister(myid)
    add_log_line(gb,"Engine (" + et + ") with id " + myid + " unregistered",CYAN)
    return True

def _unregister(myid):
    Commands.lock.acquire()
    try:
        del Commands.active_engines[myid]
        et = Commands.engine_types.pop(myid)
        cmd = Commands.pending_command.pop(myid)
        del Commands.engine_stats[myid]
        if(cmd):
            Commands.command_counter += 1
    except KeyError:
        add_log_line(gb,"Unregister request for unknown engine id: " + myid,RED)
        et = "unknown"
    finally:
        Commands.lock.release()
    return et
   


def thread_start_server(port):
    Commands.lock = threading.RLock()
    server = SimpleXMLRPCServer(("", int(port)),logRequests=False)
    server.register_function(register)
    server.register_function(unregister)
    server.register_function(poll)
    Commands.server = server
    server.serve_forever()


def thread_check_commands():
    while 1:
        Commands.lock.acquire()
        try:
            active_engines = Commands.active_engines.copy()
            engine_types = Commands.engine_types.copy()
            engine_stats = Commands.engine_stats.copy()
            
            if(Commands.command_total):
                set_progress_bar(gb,float(Commands.command_counter) / float(Commands.command_total))
                if(Commands.command_total <= Commands.command_counter):
                    Commands.command_total = 0
                    Commands.command_counter = 0
                    add_log_line(gb,Commands.command_message,GREEN)
                    set_status_message(gb,"Command complete",GREEN)
                    stop_progress_bar(gb)
        finally:
            Commands.lock.release()
        now = time.time()
        for eid,last_seen_time in active_engines.iteritems():
            if now - last_seen_time > TIMEOUT:
                et = _unregister(eid)
                add_log_line(gb,"Engine (" + et + ") with id " + eid + " timed out",RED)
        
        engine_type_count = defaultdict(int)
        for engine,type in engine_types.iteritems():
            engine_type_count[type] += 1
        
        et_stats = defaultdict(list)
        for engine,stats in engine_stats.iteritems():
            type = engine_types[engine]
            et_stats[type].append(stats)
        
        engine_type_stats = dict()
        for type,statlist in et_stats.iteritems():
            cpu = [stat[0] for stat in statlist]
            mem = [stat[1] for stat in statlist]
            memphys = [stat[2] for stat in statlist]
            memvirt = [stat[3] for stat in statlist]

            cpu_mean = float(numpy.mean(cpu))/100.0
            memp_mean = float(numpy.mean(mem))/100.0
            memp_max = float(numpy.max(mem))/100.0
            mem_mean = float(numpy.mean(memvirt))
            mem_max = float(numpy.mean(memvirt))

            engine_type_stats[type] = (cpu_mean,memp_mean,memp_max,mem_mean,mem_max)

        status.lock.acquire()
        try:
            status.engine_type_count = engine_type_count
            status.engine_type_stats = engine_type_stats
            #also monitor controller cpu usage
            if(status.controller_monitor):
                status.controller_cpu = status.controller_monitor.get_cpu_percent()
            if(status.taskclient):
                status.queue_status = status.taskclient.queue_status()
        finally:
            status.lock.release()
        time.sleep(5)


def start_server(port):
    t = threading.Thread(target=thread_start_server,args=(port,))
    t.daemon=True
    t.start()
    t = threading.Thread(target=thread_check_commands)
    t.daemon=True
    t.start()


def thread_grid_monitor(status):
    while 1:
        status.lock.acquire()
        try:
            if(status.grid_engines):
                grid_engines = list(status.grid_engines)
                grid_engines_count = sum(status.grid_engines_count)
                if(not grid_engines_count or (status.grid_count_status['running'] == grid_engines_count and status.grid_count_status['running'] == status.engine_type_count['GRID'])):
                    update = False #no need to keep spamming
                else:
                    update = True
            else:
                update = False
                grid_engines = None
                grid_engines_count = 0
                status.grid_status = dict()
                status.grid_count_status = defaultdict(int)
        finally:
            status.lock.release()
       
        if(update):
            idstatus = dict()
            countstatus = defaultdict(int)
            for id in grid_engines:
                cmd = "/usr/bin/python /opt/glite/bin/glite-wms-job-status --noint -v 0 " + id
                args = shlex.split(cmd)
                env = os.environ.copy()
                del env['PYTHONPATH']
                r = Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env).communicate()
                if(r[1]):   
                    add_log_line(gb,"Error while inquiring GRID status:",RED)
                    for line in r[1].split('\n'):
                        add_log_line(gb,line,RED)
                else:
                    res = r[0].split('\n')
                    skipfirst = True
                    for line in res:
                        if('Status' in line):
                            field = line.split(': ')[1]
                            if('info' in line):
                                id = field
                            else:
                                state = field.strip()
                                if(skipfirst):
                                    skipfirst = False
                                else:
                                    idstatus[id] = state.lower()
            for state in idstatus.itervalues():
                countstatus[state] += 1
                    
            status.lock.acquire()
            try:
                status.grid_status = idstatus
                status.grid_count_status = countstatus
            finally:
                status.lock.release()
                    

        time.sleep(60)


def start_grid_monitor(status):
    t = threading.Thread(target=thread_grid_monitor,args=(status,))
    t.daemon=True
    t.start()


def main(scr, *args, **kwds):
    opts,args = getopt.getopt(sys.argv[1:],"e:a:p:i:",["env=","address=","port=","iport="])

    envname = "grid:/enhance.dist"
    address = "gb-ui-tud.ewi.tudelft.nl"
    iport   = "30023"
    port    = "30024"
    for o,a in opts:
        if o in ('-e', '--env'):
            envname = a
        elif o in ('-a', '--address'):
            address = a
        elif o in ('-p', '--port'):
            port = a
        elif o in ('-i', '--iport'):
            iport = a
    
    init_status(status,envname,address,port,iport)
    gb.scr = scr
    gb.log_file = open('cluster.log','w')

    build_windows(gb)
    t = start_status_display(gb,status)
    start_server(port)
    start_grid_monitor(status)
    
    try:
        while 1:
            c = gb.scr.getch()

            if c == ord('q'):  #QUIT
                stop_controller(gb,status)
                stop_status_display(status,t)
                gb.scr.refresh()
                break
            elif c == ord('w'): #RELOAD ENGINES
                reload_engines(gb,status)

            elif c == ord('e'): #RESTART ENGINES
                restart_engines(gb,status)
            
            elif c == ord('r'): #REFRESH SCREEN
                build_windows(gb)
                set_status_message(gb,"Screen refreshed",GREEN)

            elif c == ord('a'): #START CONTROLLER
                if(not controller_online(status)):
                    start_controller(gb,status)
                else:
                    set_status_message(gb,"Controller already started",YELLOW)

            elif c == ord('z'):  #STOP CONTROLLER
                stop_controller(gb,status)

            elif c == ord('s'):  #START local engines
                cmd = get_number(gb, "Number of engines (def=4): ",4)
                if(not cmd is None):
                    start_locals(gb,status,cmd)

            elif c == ord('x'):  #STOP local engines
                cmd = get_number(gb, "Number of engines (def=all): ",len(status.local_engines))
                if(not cmd is None):
                    stop_locals(gb,status,cmd)
            elif c == ord('d'):  #START PBS engines
                cmd = get_number(gb, "Number of engines (def=4): ",4)
                if(not cmd is None):
                    start_pbs(gb,status,cmd)

            elif c == ord('c'):  #STOP PBS engines
                cmd = get_number(gb, "Number of engines (def=all): ",len(status.pbs_engines))
                if(not cmd is None):
                    stop_pbs(gb,status,cmd)
            elif c == ord('f'):  #START GRID engines
                cmd = get_number(gb, "Number of engines (def=4): ",4)
                if(not cmd is None):
                    start_grid(gb,status,cmd)

            elif c == ord('v'):  #STOP GRID engines
                cmd = get_number(gb, "Number of engines (def=all): ",sum(status.grid_engines_count))
                if(not cmd is None):
                    stop_grid(gb,status,cmd)
    except Exception:
        add_log_line(gb,"EXCEPTION!!! EMERGENCY SHUTDOWN IN PROGRESS!",RED)
        try:
            stop_status_display(status,t)
        except Exception:
            pass
        try:
            stop_controller(gb,status)
        except Exception:
            pass
        raise
        
    time.sleep(2) #wait for all paint ops to finish
curses.wrapper(main)
