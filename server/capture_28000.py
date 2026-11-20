import socket, os, datetime
HOST='0.0.0.0'; PORT=28000
OUT=r'D:\BaiduNetdiskDownload\CSXY_work\evidence\baseapp_frames'
os.makedirs(OUT,exist_ok=True)
print('listening %s:%d' % (HOST,PORT), flush=True)
seq=0
s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); s.bind((HOST,PORT)); s.listen(8)
while True:
    c,a=s.accept(); seq+=1
    stamp=datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    path=os.path.join(OUT,'conn_%03d_%s.bin'%(seq,stamp))
    print('accepted %s -> %s' % (a,path), flush=True)
    total=0
    with open(path,'wb') as f:
        c.settimeout(10)
        try:
            while True:
                b=c.recv(65536)
                if not b: break
                f.write(b); total+=len(b)
                print('  recv %d bytes (total %d)'%(len(b),total),flush=True)
        except Exception as e: print('  stop:',repr(e),flush=True)
    c.close(); print('closed total=%d'%total,flush=True)
