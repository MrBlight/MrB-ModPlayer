#!/usr/bin/env python3
# MrB-ModPlay 0.8.0
import sys,struct,threading,time,math,glob,platform,queue
from pathlib import Path
try:import numpy as np,sounddevice as sd
except ImportError:sys.exit("pip install sounddevice numpy")

SR=44100
# Amiga clock (PAL) for MOD period math only
PAL=7093789.2
# S3M uses its own clock constant
S3M_CLK=14317056.0
# XM amiga mode uses this exact constant (8363*1712)
XM_APC=14317456.0
# 64-entry sine table for vibrato/tremolo (values -127..127)
SIN=[int(127*math.sin(math.pi*2*i/64))for i in range(64)]
EXTS={'.mod','.s3m','.xm','.it'}
IS_WIN=platform.system()=='Windows'
BLKSIZE=2048;QMAX=32
# MOD/XM amiga period table for C-B (octave reference)
_APT=[1712,1616,1524,1440,1356,1280,1208,1140,1076,1016,960,907]
MOD_TAGS={b'M.K.':4,b'M!K!':4,b'FLT4':4,b'4CHN':4,b'6CHN':6,b'8CHN':8,
 b'FLT8':8,b'2CHN':2,b'10CH':10,b'12CH':12,b'16CH':16,b'32CH':32}

# ── platform keyboard ─────────────────────────────────────────────────────────
if IS_WIN:
 import msvcrt
 kbhit=msvcrt.kbhit
 def getch():
  c=msvcrt.getch()
  return c.decode('utf-8',errors='ignore') if isinstance(c,bytes) else c
 raw_on=raw_off=lambda:None
else:
 import tty,termios,select
 _fd=sys.stdin.fileno();_old=None
 def raw_on():
  global _old
  try:_old=termios.tcgetattr(_fd);tty.setraw(_fd)
  except:pass
 def raw_off():
  try:
   if _old:termios.tcsetattr(_fd,termios.TCSADRAIN,_old)
  except:pass
 def kbhit():
  try:r,*_=select.select([sys.stdin],[],[],0);return bool(r)
  except:return False
 def getch():return sys.stdin.read(1)

# ── data types ────────────────────────────────────────────────────────────────
class Smp:
 __slots__=('name','vol','pan','ft','ls','ll','c5','relnote','data')
 def __init__(self):
  self.name='';self.vol=64;self.pan=128;self.ft=0
  self.ls=0;self.ll=0;self.c5=8363;self.relnote=0
  self.data=np.zeros(0,dtype=np.float32)

class Trk:
 __slots__=('snum','eff','prm','freq','tfreq','pos','per','bper','s3mper',
            'vol','pan','on','ptgt','pspd','vp','vs','vd')
 def __init__(self):
  self.snum=self.eff=self.prm=0
  self.freq=self.tfreq=self.pos=0.0
  self.per=self.bper=0          # amiga period (MOD/XM-amiga)
  self.s3mper=0                 # ST3 period (S3M portamento math)
  self.vol=64;self.pan=128;self.on=False
  self.ptgt=self.pspd=self.vp=self.vs=self.vd=0

class Mod:
 def __init__(self):
  self.fmt='?';self.title='';self.smp=[Smp()]   # smp[0] = dummy
  self.orders=[];self.pats=[];self.nc=4;self.sl=0
  self.bpm=125;self.spd=6;self.ntbl=[];self.linear=True
 def row(self,o,r):return self.pats[self.orders[o]][r]

# ── sample converters ─────────────────────────────────────────────────────────
def _u8f(r):(np.frombuffer(r,dtype=np.uint8).astype(np.float32)-128)/128
_u8f=lambda r:(np.frombuffer(r,dtype=np.uint8).astype(np.float32)-128.0)/128.0
_s8f=lambda r:np.frombuffer(r,dtype=np.int8).astype(np.float32)/128.0
_s16f=lambda r:np.frombuffer(r,dtype='<i2').astype(np.float32)/32768.0

# ── loaders ───────────────────────────────────────────────────────────────────
def _load_mod(data):
 m=Mod();m.fmt='MOD';m.linear=False
 m.title=data[:20].rstrip(b'\x00').decode('latin-1',errors='replace')
 tag=data[1080:1084];ns=15;nc=4
 if tag in MOD_TAGS:nc=MOD_TAGS[tag];ns=31
 elif len(tag)==4 and tag[:2].isdigit() and tag[2:4]==b'CH':
  try:nc=int(tag[:2]);ns=31
  except:pass
 m.nc=nc;off=20;slens=[]
 for _ in range(ns):
  s=Smp()
  s.name=data[off:off+22].rstrip(b'\x00').decode('latin-1',errors='replace')
  slens.append(struct.unpack_from('>H',data,off+22)[0]*2)
  ft=data[off+24]&0xF;s.ft=ft if ft<8 else ft-16
  s.vol=min(64,data[off+25])
  s.ls=struct.unpack_from('>H',data,off+26)[0]*2
  s.ll=struct.unpack_from('>H',data,off+28)[0]*2
  if s.ll<=2:s.ll=0
  m.smp.append(s);off+=30
 m.sl=data[off];off+=2
 m.orders=list(data[off:off+128]);off+=128
 if ns==31:off+=4
 npats=max(m.orders[:max(1,m.sl)])+1 if m.sl else 1
 for _ in range(npats):
  p=[]
  for _ in range(64):
   row=[]
   for _ in range(nc):
    if off+4>len(data):row.append((0,0,0,0));continue
    b=data[off:off+4];off+=4
    snum=(b[0]&0xF0)|(b[2]>>4)
    per=((b[0]&0xF)<<8)|b[1]
    eff=(b[2]&0xF);prm=b[3]
    row.append((snum,per,eff,prm))
   p.append(row)
  m.pats.append(p)
 for s,ln in zip(m.smp[1:],slens):
  if ln>0 and off+ln<=len(data):
   s.data=_s8f(data[off:off+ln]);off+=ln
 return m

def _load_s3m(data):
 m=Mod();m.fmt='S3M';m.linear=False
 m.title=data[:28].rstrip(b'\x00').decode('latin-1',errors='replace')
 ordnum=struct.unpack_from('<H',data,0x20)[0]
 ns=struct.unpack_from('<H',data,0x22)[0]
 np2=struct.unpack_from('<H',data,0x24)[0]
 # Ffv at 0x2A: 1=signed samples(old), 2=unsigned samples(standard)
 ffv=struct.unpack_from('<H',data,0x2A)[0] if len(data)>0x2B else 2
 signed_smp=(ffv==1)
 m.spd=max(1,data[0x31]) if len(data)>0x32 else 6
 m.bpm=max(32,data[0x32]) if len(data)>0x33 else 125
 m.orders=[o for o in data[0x60:0x60+ordnum] if o<254]
 m.sl=len(m.orders)
 base=0x60+ordnum
 psmp=[struct.unpack_from('<H',data,base+i*2)[0]*16 for i in range(ns)]
 ppat=[struct.unpack_from('<H',data,base+ns*2+i*2)[0]*16 for i in range(np2)]
 for pp in psmp:
  s=Smp()
  if pp and pp+0x50<=len(data) and data[pp]==1:  # type 1 = PCM sample
   s.name=data[pp+0x30:pp+0x30+28].rstrip(b'\x00').decode('latin-1',errors='replace')
   # MemSeg: 3-byte parapointer at pp+0x0D..0x0F (lo,mid,hi byte order)
   # lo-word at pp+0x0D (little-endian 16-bit), hi-byte at pp+0x0F
   dp=(data[pp+0x0F]<<16|struct.unpack_from('<H',data,pp+0x0D)[0])*16
   # Length, LoopBeg, LoopEnd are split 32-bit: lo-word then hi-word
   slen=struct.unpack_from('<I',data,pp+0x10)[0]
   sls=struct.unpack_from('<I',data,pp+0x14)[0]
   sle=struct.unpack_from('<I',data,pp+0x18)[0]
   s.vol=min(64,data[pp+0x1C])
   flgs=data[pp+0x1F];is16=(flgs&4)!=0;has_loop=(flgs&1)!=0
   nb=2 if is16 else 1
   s.ls=sls;s.ll=sle-sls if has_loop and sle>sls+2 else 0
   # C2Spd at pp+0x20: only lower 16-bits used by ST3
   s.c5=struct.unpack_from('<H',data,pp+0x20)[0] or 8363
   if slen>0 and dp>0 and dp+slen*nb<=len(data):
    raw=data[dp:dp+slen*nb]
    if is16:s.data=_s16f(raw)
    elif signed_smp:s.data=_s8f(raw)
    else:s.data=_u8f(raw)
  m.smp.append(s)
 cset=set()
 for pp in ppat:
  pat=[[None]*32 for _ in range(64)]
  if pp and pp+2<=len(data):
   off=pp+2;row=0
   while row<64 and off<len(data):
    b=data[off];off+=1
    if b==0:row+=1;continue
    ch=b&0x1F;cset.add(ch)
    note=ins=eff=prm=0;vol=-1
    if b&0x20 and off+1<len(data):note=data[off];ins=data[off+1];off+=2
    if b&0x40 and off<len(data):vol=data[off];off+=1
    if b&0x80 and off+1<len(data):eff=data[off];prm=data[off+1];off+=2
    if row<64 and ch<32:pat[row][ch]=(note,ins,vol,eff,prm)
  m.pats.append(pat)
 m.nc=max(cset)+1 if cset else 4
 return m

def _load_xm(data):
 m=Mod();m.fmt='XM'
 m.title=data[17:37].rstrip(b'\x00').decode('latin-1',errors='replace')
 hs=struct.unpack_from('<I',data,60)[0]           # header size from offset 60
 m.sl=min(struct.unpack_from('<H',data,64)[0],255)
 m.nc=min(struct.unpack_from('<H',data,68)[0],32)
 np2=struct.unpack_from('<H',data,70)[0]
 ni=struct.unpack_from('<H',data,72)[0]
 m.linear=bool(struct.unpack_from('<H',data,74)[0]&1)
 m.spd=max(1,struct.unpack_from('<H',data,76)[0])
 m.bpm=max(32,struct.unpack_from('<H',data,78)[0])
 m.orders=list(data[80:80+256])[:m.sl]
 off=60+hs  # patterns start here
 # -- patterns --
 for _ in range(np2):
  if off+9>len(data):break
  phlen=struct.unpack_from('<I',data,off)[0]
  nrows=max(1,struct.unpack_from('<H',data,off+5)[0])
  pdsize=struct.unpack_from('<H',data,off+7)[0]
  pdata_off=off+max(phlen,9)
  off=pdata_off+pdsize
  pat=[]
  if pdsize==0:
   for _ in range(nrows):pat.append([(0,0,0xFF,0,0)]*m.nc)
  else:
   raw=data[pdata_off:pdata_off+pdsize];ri=0
   for _ in range(nrows):
    row=[]
    for _ in range(m.nc):
     if ri>=len(raw):row.append((0,0,0xFF,0,0));continue
     b=raw[ri];ri+=1;note=ins=eff=prm=0;vol=0xFF
     if b&0x80:
      if b&1 and ri<len(raw):note=raw[ri];ri+=1
      if b&2 and ri<len(raw):ins=raw[ri];ri+=1
      if b&4 and ri<len(raw):vol=raw[ri];ri+=1
      if b&8 and ri<len(raw):eff=raw[ri];ri+=1
      if b&16 and ri<len(raw):prm=raw[ri];ri+=1
     else:
      # uncompressed: note byte followed by 4 more
      note=b
      if ri+4<=len(raw):ins,vol,eff,prm=raw[ri],raw[ri+1],raw[ri+2],raw[ri+3];ri+=4
      elif ri<len(raw):ins=raw[ri];ri+=1
     row.append((note,ins,vol,eff,prm))
    pat.append(row)
  m.pats.append(pat)
 # -- instruments --
 for _ in range(ni):
  if off+4>len(data):break
  istart=off
  isz=struct.unpack_from('<I',data,istart)[0]
  nsmp=struct.unpack_from('<H',data,istart+27)[0] if istart+28<=len(data) else 0
  # sample headers follow at istart+isz (the full instrument header size)
  shdr_off=istart+max(isz,29)
  if nsmp==0:
   # no samples: add empty ntbl, DON'T add any Smp
   m.ntbl.append([0]*96)
   off=shdr_off
   continue
  sb=len(m.smp)  # index of first sample for this instrument
  # note->sample table: 96 bytes at istart+33 (0-based sample index within instrument)
  rnt=[0]*96
  if istart+129<=len(data):
   rnt=list(data[istart+33:istart+129])
  m.ntbl.append([sb+rnt[j] if j<96 and rnt[j]<nsmp else 0 for j in range(96)])
  # read all sample headers first (they come before sample data)
  shdrs=[]
  shp=shdr_off
  for _ in range(nsmp):
   if shp+40>len(data):break
   sl2,sls,sll=struct.unpack_from('<III',data,shp)   # all in BYTES
   sv=data[shp+12]
   sft=data[shp+13];sft=sft if sft<128 else sft-256  # signed
   sf=data[shp+14];sp=data[shp+15]
   srn=data[shp+16];srn=srn if srn<128 else srn-256  # signed
   shdrs.append((sl2,sls,sll,sv,sft,sf,sp,srn));shp+=40
  # sample data follows all sample headers
  sdp=shp
  for sl2,sls,sll,sv,sft,sf,sp,srn in shdrs:
   s=Smp();s.vol=sv;s.ft=sft;s.pan=sp;s.relnote=srn
   is16=(sf&16)!=0;loop_type=(sf&3)
   nb=2 if is16 else 1
   # sl2, sls, sll are in BYTES -- convert to sample counts
   nsamp=sl2//nb;ls_s=sls//nb;ll_s=sll//nb
   raw=data[sdp:sdp+sl2];sdp+=sl2   # advance by sl2 bytes
   if raw:
    # XM samples are stored as delta-encoded signed values
    arr=np.frombuffer(raw,dtype='<i2' if is16 else np.int8).copy().astype(np.int32)
    arr=np.cumsum(arr)
    if is16:
     arr=np.clip(arr,-32768,32767).astype(np.float32)/32768.0
    else:
     arr=(arr&0xFF).astype(np.int8).astype(np.float32)/128.0
    s.data=arr
   s.ls=ls_s;s.ll=ll_s if loop_type and ll_s>2 else 0
   m.smp.append(s)
  off=sdp
 return m

def _load_it(data):
 m=Mod();m.fmt='IT';m.linear=True
 m.title=data[4:30].rstrip(b'\x00').decode('latin-1',errors='replace')
 # IT header layout (all little-endian):
 # 0x00: 'IMPM', 0x04-0x1D: title
 # 0x20: OrdNum, 0x22: InsNum, 0x24: SmpNum, 0x26: PatNum
 # 0x28: Cwt/v, 0x2A: Cmwt, 0x2C: Flags, 0x2E: Special
 # 0x30: GV, 0x31: MV, 0x32: IS, 0x33: IT (initial speed/tempo)
 ordnum=struct.unpack_from('<H',data,0x20)[0]
 ni=struct.unpack_from('<H',data,0x22)[0]
 ns_=struct.unpack_from('<H',data,0x24)[0]
 np2=struct.unpack_from('<H',data,0x26)[0]
 it_flags=struct.unpack_from('<H',data,0x2C)[0]
 m.linear=bool(it_flags&8)     # bit 3: linear frequency slides
 use_instruments=bool(it_flags&4)  # bit 2: use instruments
 m.spd=max(1,data[0x32]) if len(data)>0x32 else 6
 m.bpm=max(32,data[0x33]) if len(data)>0x33 else 125
 m.orders=[o for o in data[0xC0:0xC0+ordnum] if o<254]
 m.sl=len(m.orders)
 # Pointer tables: instruments, samples, patterns (4-byte offsets)
 base=0xC0+ordnum
 ins_p=[struct.unpack_from('<I',data,base+i*4)[0] for i in range(ni)]
 base2=base+ni*4
 smp_p=[struct.unpack_from('<I',data,base2+i*4)[0] for i in range(ns_)]
 base3=base2+ns_*4
 pat_p=[struct.unpack_from('<I',data,base3+i*4)[0] for i in range(np2)]
 m.nc=64
 # -- instruments --
 for pp in ins_p:
  ntbl=[0]*120  # 0=no sample; 1-based sample number otherwise
  if pp and pp+0x140<=len(data) and data[pp:pp+4]==b'IMPI':
   # Note-sample/keyboard table at pp+0x40: 120 pairs of (note, sample)
   # note: 0-119 (C-0 to B-9), sample: 1-99 (1-based), 0=no sample
   for j in range(120):
    idx=pp+0x40+j*2
    if idx+1<len(data):ntbl[j]=data[idx+1]   # sample number (1-based)
  m.ntbl.append(ntbl)
 # -- samples --
 for pp in smp_p:
  s=Smp()
  if pp and pp+0x50<=len(data) and data[pp:pp+4]==b'IMPS':
   # IMPS layout:
   # +0x00: 'IMPS', +0x04: DOS filename (12 bytes), +0x10: 0x00
   # +0x11: GvL (global vol 0-64), +0x12: Flg, +0x13: Vol (default vol 0-64)
   # +0x14: Sample name (26 bytes incl NUL) .. +0x2D
   # +0x2E: Cvt, +0x2F: DfP
   # +0x30: Length(4), +0x34: LoopBegin(4), +0x38: LoopEnd(4), +0x3C: C5Speed(4)
   # +0x40: SusLBeg(4), +0x44: SusLEnd(4), +0x48: SamplePointer(4)
   gvl=data[pp+0x11]
   flg=data[pp+0x12]
   vol=data[pp+0x13]
   s.name=data[pp+0x14:pp+0x2E].rstrip(b'\x00').decode('latin-1',errors='replace')
   cvt=data[pp+0x2E]
   # fold global vol into sample vol
   s.vol=min(64,vol*gvl//64) if gvl<64 else min(64,vol)
   if not(flg&1):m.smp.append(s);continue   # no sample data associated
   if flg&8:m.smp.append(s);continue         # compressed - not supported here
   slen=struct.unpack_from('<I',data,pp+0x30)[0] if pp+0x34<=len(data) else 0
   lb=struct.unpack_from('<I',data,pp+0x34)[0] if pp+0x38<=len(data) else 0
   le=struct.unpack_from('<I',data,pp+0x38)[0] if pp+0x3C<=len(data) else 0
   s.c5=max(256,struct.unpack_from('<I',data,pp+0x3C)[0]) if pp+0x40<=len(data) else 8363
   dp=struct.unpack_from('<I',data,pp+0x48)[0] if pp+0x4C<=len(data) else 0
   is16=(flg&2)!=0;has_loop=(flg&16)!=0;nb=2 if is16 else 1
   # Cvt bit0: 0=unsigned, 1=signed; bit2: 0=PCM, 1=delta
   signed_s=bool(cvt&1);delta=bool(cvt&4)
   s.ls=lb//nb;s.ll=(le-lb)//nb if has_loop and le>lb else 0
   if slen>0 and dp>0 and dp+slen*nb<=len(data):
    raw=data[dp:dp+slen*nb]
    if delta:
     if is16:
      arr=np.frombuffer(raw,dtype='<i2').copy().astype(np.int32)
      s.data=np.clip(np.cumsum(arr),-32768,32767).astype(np.float32)/32768.0
     else:
      arr=np.frombuffer(raw,dtype=np.int8).copy().astype(np.int16)
      s.data=(np.cumsum(arr)&0xFF).astype(np.int8).astype(np.float32)/128.0
    elif is16:
     if signed_s:s.data=_s16f(raw)
     else:s.data=(np.frombuffer(raw,dtype='<u2').astype(np.float32)-32768)/32768.0
    else:
     if signed_s:s.data=_s8f(raw)
     else:s.data=_u8f(raw)
  m.smp.append(s)
 # -- patterns --
 for pp in pat_p:
  nrows=64
  pat=[]
  if pp and pp+8<=len(data):
   plen=struct.unpack_from('<H',data,pp)[0]
   nrows=min(max(1,struct.unpack_from('<H',data,pp+2)[0]),200)
   rd=data[pp+8:pp+8+plen];ri=0;row=0
   lm=[0]*64;ln=[0xFF]*64;li=[0]*64;lv=[0xFF]*64;le2=[0]*64;lp=[0]*64
   cur=[[0xFF,0,0xFF,0,0] for _ in range(64)]  # [note,ins,vol,eff,prm]
   rows=[]
   while row<nrows and ri<len(rd):
    b=rd[ri];ri+=1
    if b==0:
     rows.append([tuple(cur[ch]) for ch in range(64)])
     # reset current row
     cur=[[0xFF,0,0xFF,0,0] for _ in range(64)]
     row+=1;continue
    ch=(b-1)&63
    if b&128 and ri<len(rd):lm[ch]=rd[ri];ri+=1
    mask=lm[ch]
    # note
    if mask&1 and ri<len(rd):ln[ch]=rd[ri];ri+=1
    if mask&16:pass   # reuse last note (already in ln[ch])
    note=ln[ch] if (mask&1 or mask&16) else 0xFF
    # instrument
    if mask&2 and ri<len(rd):li[ch]=rd[ri];ri+=1
    ins=li[ch] if (mask&2 or mask&32) else 0
    # vol
    if mask&4 and ri<len(rd):lv[ch]=rd[ri];ri+=1
    vol=lv[ch] if (mask&4 or mask&64) else 0xFF
    # eff
    if mask&8 and ri+1<len(rd):le2[ch]=rd[ri];lp[ch]=rd[ri+1];ri+=2
    eff=le2[ch] if (mask&8 or mask&128) else 0
    prm=lp[ch] if (mask&8 or mask&128) else 0
    cur[ch]=[note,ins,vol,eff,prm]
   # flush remaining rows
   while row<nrows:
    rows.append([tuple(cur[ch]) for ch in range(64)])
    cur=[[0xFF,0,0xFF,0,0] for _ in range(64)]
    row+=1
   pat=rows
  else:
   pat=[([(0xFF,0,0xFF,0,0)]*64) for _ in range(nrows)]
  m.pats.append(pat)
 return m

def load(path):
 data=Path(path).read_bytes();ext=Path(path).suffix.lower()
 if ext=='.s3m':return _load_s3m(data)
 if ext=='.xm':return _load_xm(data)
 if ext=='.it':return _load_it(data)
 return _load_mod(data)

# ── frequency helpers ─────────────────────────────────────────────────────────

def _af(per):
 """Amiga period -> Hz (MOD only)"""
 return PAL/(per*2.0) if per>0 else 0.0

def _mod_ft(per,ft):
 """Apply MOD finetune (-8..+7) to amiga period. Returns adjusted period."""
 return max(1,int(round(per/(2.0**(ft/96.0))))) if ft and per else per

def _xm_lin(note,ft,rn):
 """XM linear frequency table.
 note: 1-based (1=C-0..96=B-7), ft: signed -128..+127, rn: relnote signed.
 Returns sample playback rate in Hz."""
 n=note+rn
 if not(1<=n<=120):return 0.0
 # Freq = 8363 * 2^((n*64 + ft/2 - 3904) / 768)
 # Derived from spec: Period=7680-n*64-ft/2, Freq=8363*2^((4608-Period)/768)
 # normalized so C-5 (n=61) with ft=0,rn=0 gives 8363 Hz
 return 8363.0*2.0**((n*64+ft//2-3904)/768.0)

def _xm_amiga(note,ft,rn):
 """XM amiga frequency table.
 note: 1-based, ft: signed -128..+127, rn: relnote.
 Returns sample playback rate in Hz."""
 n=note+rn-1  # 0-based
 if not(0<=n<=119):return 0.0
 semi=n%12;oct_=n//12
 p0=_APT[semi]
 p1=_APT[(semi+1)%12] if semi<11 else _APT[0]//2
 if ft>=0:period=p0+int((p0-p1)*ft/128.0)
 else:
  pp2=_APT[(semi-1)%12] if semi>0 else _APT[11]*2
  period=p0+int((pp2-p0)*(-ft)/128.0)
 # XM reference octave is 5 (C-5=note61=n0=60 -> oct_=5, period=1712)
 if oct_>=5:period>>=oct_-5
 else:period<<=5-oct_
 # XM amiga freq formula from spec: 8363*1712/period
 return XM_APC/max(1,period)

def _s3m_freq(note,c5):
 """S3M packed note byte (hi-nibble=octave, lo-nibble=semitone) -> Hz.
 c5: sample's C5 speed (sample rate at C-5, i.e. oct=5, semi=0)."""
 if not note or note>=254:return 0.0
 # note index = oct*12 + semi; C-5 reference = 5*12+0 = 60
 idx=(note>>4)*12+(note&0xF)
 return c5*2.0**((idx-60)/12.0)

def _it_freq(note,c5):
 """IT note (0=C-0 .. 119=B-9) -> Hz. c5: sample rate at C-5 (note 60)."""
 if not(0<=note<=119):return 0.0
 return float(c5)*2.0**((note-60)/12.0)

# ── mixer ─────────────────────────────────────────────────────────────────────
def _mix(c,mod,n):
 """Mix n output samples from channel c. Returns float32 array or None."""
 if not c.on or not c.snum or c.freq<=0:return None
 if c.snum>=len(mod.smp):return None
 s=mod.smp[c.snum];d=s.data;dl=len(d)
 if not dl:return None
 vol=c.vol/64.0;step=c.freq/SR
 ll=s.ll;ls=s.ls;le=ls+ll
 loop=ll>2 and le<=dl
 out=np.zeros(n,np.float32)
 pos=c.pos;wr=0
 while wr<n:
  rem=n-wr
  if loop:
   if pos>=le:pos=ls+(pos-ls)%ll
   av=min(rem,max(1,int((le-pos)/step)+1))
  else:
   if pos>=dl:c.on=False;break
   av=min(rem,max(1,int((dl-pos)/step)+1))
  if av<=0:c.on=False;break
  idx=np.arange(av,dtype=np.float64)*step+pos
  if loop:idx=ls+np.mod(idx-ls,ll)
  else:idx=np.clip(idx,0.0,dl-1.0001)
  ip=idx.astype(np.int32)
  ip1=np.minimum(ip+1,dl-1)
  frac=(idx-ip).astype(np.float32)
  chunk=(d[ip]+frac*(d[ip1]-d[ip]))*vol
  end=min(wr+av,n)
  out[wr:end]+=chunk[:end-wr]
  pos+=av*step;wr=end
  if not loop and pos>=dl:c.on=False;break
 c.pos=pos
 return out

# ── player ────────────────────────────────────────────────────────────────────
class Player:
 def __init__(self,mod):
  self.mod=mod;self.nc=mod.nc
  self.ch=[Trk() for _ in range(mod.nc)]
  self.op=self.row=self.tick=self._tp=0
  self.spd=mod.spd;self.bpm=mod.bpm
  self._spt=self._gspt()
  self.playing=self.paused=self.ended=False
  self._lk=threading.Lock();self._st=None
  self._pb=self._pj=-1;self._lsr=self._lsc=0
  self._q=queue.Queue(maxsize=QMAX);self._wt=None
  self._ipan()

 def _ipan(self):
  if self.mod.fmt=='MOD':
   pans=[0,255,255,0]
   for i,c in enumerate(self.ch):c.pan=pans[i%4]
  else:
   for c in self.ch:c.pan=128

 def _gspt(self):
  """Samples per tick at current BPM."""
  return max(1,int(SR*60/(self.bpm*24)))

 # ── note frequency helpers ──────────────────────────────────────────────────

 def _freq(self,note,c):
  """Compute frequency for a new note on channel c. note meaning depends on fmt."""
  fmt=self.mod.fmt
  s=self.mod.smp[c.snum] if c.snum and c.snum<len(self.mod.smp) else None
  if fmt=='MOD':
   return _af(_mod_ft(note,s.ft if s else 0)) if note else 0.0
  if fmt=='S3M':
   return _s3m_freq(note,s.c5 if s else 8363)
  if fmt=='XM':
   ft=s.ft if s else 0;rn=s.relnote if s else 0
   return _xm_lin(note,ft,rn) if self.mod.linear else _xm_amiga(note,ft,rn)
  if fmt=='IT':
   return _it_freq(note,s.c5 if s else 8363)
  return 0.0

 def _trig_mod(self,c,per):
  """Trigger MOD note: set period and restart sample."""
  c.per=per;c.bper=per
  c.freq=_af(per);c.tfreq=c.freq
  c.pos=0.0;c.vp=0;c.on=True

 def _trig(self,c,freq):
  """Trigger non-MOD note: set freq and restart sample."""
  c.freq=freq;c.tfreq=freq
  c.pos=0.0;c.vp=0;c.on=True
  if self.mod.fmt=='S3M' and freq>0:
   c.s3mper=int(S3M_CLK/freq)
  elif self.mod.fmt in('XM','IT') and not self.mod.linear and freq>0:
   c.per=int(XM_APC/freq);c.bper=c.per

 # ── row 0 (new row processing) ──────────────────────────────────────────────

 def _row0(self):
  if self.op>=self.mod.sl:self.ended=True;return
  fmt=self.mod.fmt
  cells=self.mod.row(self.op,self.row)
  for i,c in enumerate(self.ch):
   if i>=len(cells) or cells[i] is None:continue
   cell=cells[i]

   if fmt=='MOD':
    snum,per,eff,prm=cell
    if snum and snum<len(self.mod.smp):
     c.snum=snum;c.vol=self.mod.smp[snum].vol
    if per:
     s=self.mod.smp[c.snum] if c.snum and c.snum<len(self.mod.smp) else None
     per2=_mod_ft(per,s.ft if s else 0)
     if eff in(3,5):c.ptgt=per2  # tone porta target
     else:self._trig_mod(c,per2)
    elif eff not in(3,5):c.bper=c.per
    c.eff=eff;c.prm=prm
    if eff==3 and prm:c.pspd=prm
    elif eff==4:
     if prm>>4:c.vs=prm>>4
     if prm&0xF:c.vd=prm&0xF
    elif eff==9 and prm:c.pos=prm*256.0
    elif eff==0xB:self._pj=prm%self.mod.sl
    elif eff==0xC:c.vol=min(64,prm)
    elif eff==0xD:self._pb=(prm>>4)*10+(prm&0xF)
    elif eff==0xF:
     if prm and prm<32:self.spd=prm
     elif prm>=32:self.bpm=prm;self._spt=self._gspt()
    elif eff==0xE:
     s2,a=prm>>4,prm&0xF
     if s2==1:c.per=max(113,c.per-a);c.bper=c.per;c.freq=_af(c.per);c.tfreq=c.freq
     elif s2==2:c.per+=a;c.bper=c.per;c.freq=_af(c.per);c.tfreq=c.freq
     elif s2==6:
      if a==0:self._lsr=self.row
      elif self._lsc==0:self._lsc=a;self._pb=self._lsr;self._pj=self.op
      elif self._lsc>0:
       self._lsc-=1
       if self._lsc:self._pb=self._lsr;self._pj=self.op
     elif s2==0xA:c.vol=min(64,c.vol+a)
     elif s2==0xB:c.vol=max(0,c.vol-a)
     elif s2==0xC:c.prm=prm  # note cut on tick a (handled in tickfx)

   elif fmt=='S3M':
    note,ins,vol,eff,prm=cell
    if ins and ins<len(self.mod.smp):
     c.snum=ins;c.vol=self.mod.smp[ins].vol
    s=self.mod.smp[c.snum] if c.snum and c.snum<len(self.mod.smp) else None
    if note==254:c.on=False          # ^^ = note cut
    elif note and note!=255:
     freq=_s3m_freq(note,s.c5 if s else 8363)
     if freq>0:
      if eff==7:                     # G = tone porta: set target period
       c.ptgt=int(S3M_CLK/freq)
       if not c.s3mper:c.s3mper=int(S3M_CLK/c.freq) if c.freq>0 else c.ptgt
      else:self._trig(c,freq)
    if vol>=0:c.vol=min(64,vol)
    c.eff=eff;c.prm=prm
    if eff==1:self.spd=max(1,prm)
    elif eff==2:self._pj=prm%self.mod.sl
    elif eff==3:self._pb=(prm>>4)*10+(prm&0xF)
    elif eff==4:                     # D = volume slide (Dxy)
     vh,vl=prm>>4,prm&0xF
     if prm>=0xF0:c.vol=max(0,c.vol-vl)        # DFx = fine slide down tick 0
     elif vl==0xF:c.vol=min(64,c.vol+vh)        # DxF = fine slide up tick 0
    elif eff==5 and prm:             # E = porta down
     if prm&0xF0==0xF0:             # EFx = extra fine (1/4 unit)
      c.s3mper+=prm&0xF
      if c.s3mper:c.freq=S3M_CLK/c.s3mper;c.tfreq=c.freq
     elif prm&0xF0==0xE0:           # EEx = fine (1 unit)
      c.s3mper+=(prm&0xF)*4
      if c.s3mper:c.freq=S3M_CLK/c.s3mper;c.tfreq=c.freq
    elif eff==6 and prm:             # F = porta up
     if prm&0xF0==0xF0:
      c.s3mper=max(1,c.s3mper-(prm&0xF))
      c.freq=S3M_CLK/c.s3mper;c.tfreq=c.freq
     elif prm&0xF0==0xE0:
      c.s3mper=max(1,c.s3mper-(prm&0xF)*4)
      c.freq=S3M_CLK/c.s3mper;c.tfreq=c.freq
    elif eff==7 and prm:c.pspd=prm  # G speed memory
    elif eff==8:                     # H = vibrato
     if prm>>4:c.vs=prm>>4
     if prm&0xF:c.vd=prm&0xF
    elif eff==20:self.bpm=max(32,prm);self._spt=self._gspt()  # T = tempo

   elif fmt=='XM':
    note,ins,vol,eff,prm=cell
    # Resolve instrument -> sample via note table
    if ins and 1<=ins<=len(self.mod.ntbl):
     ntbl=self.mod.ntbl[ins-1]
     n0=note-1 if note and 0<note<=96 else (c.snum-1 if c.snum else 0)
     n0=max(0,min(95,n0))
     sidx=ntbl[n0]
     if sidx and sidx<len(self.mod.smp):
      c.snum=sidx
      sv=self.mod.smp[sidx]
      c.vol=sv.vol;c.pan=sv.pan
    elif ins and ins<len(self.mod.smp):
     c.snum=ins;sv=self.mod.smp[ins];c.vol=sv.vol;c.pan=sv.pan
    if note and 0<note<=96:
     freq=self._freq(note,c)
     if freq>0:
      if eff in(3,5):  # tone porta: set target
       if self.mod.linear:c.ptgt=freq
       else:c.ptgt=int(XM_APC/freq)
      else:
       self._trig(c,freq)
    elif note==97:c.on=False          # key-off
    # volume column
    if 0x10<=vol<=0x50:c.vol=vol-0x10
    elif 0x60<=vol<=0x6F:c.vol=max(0,c.vol-(vol&0xF))    # fine vol down
    elif 0x70<=vol<=0x7F:c.vol=min(64,c.vol+(vol&0xF))   # fine vol up
    elif 0x80<=vol<=0x8F:c.vol=max(0,c.vol-(vol&0xF))    # vol slide down
    elif 0x90<=vol<=0x9F:c.vol=min(64,c.vol+(vol&0xF))   # vol slide up
    elif 0xA0<=vol<=0xAF:c.vs=vol&0xF                     # set vib speed
    elif 0xC0<=vol<=0xCF:c.pan=((vol&0xF)<<4)|((vol&0xF))# set pan
    elif 0xF0<=vol<=0xFF:                                   # tone porta (vol col)
     tgt_freq=self._freq(note,c) if note and 0<note<=96 else 0
     if tgt_freq>0:
      if self.mod.linear:c.ptgt=tgt_freq
      else:c.ptgt=int(XM_APC/tgt_freq)
      if prm:c.pspd=prm
    c.eff=eff;c.prm=prm
    if eff==3 and prm:c.pspd=prm
    elif eff==4:
     if prm>>4:c.vs=prm>>4
     if prm&0xF:c.vd=prm&0xF
    elif eff==9 and prm:c.pos=prm*256.0
    elif eff==0xB:self._pj=prm%self.mod.sl
    elif eff==0xC:c.vol=min(64,prm)
    elif eff==0xD:self._pb=(prm>>4)*10+(prm&0xF)
    elif eff==0xF:
     if prm and prm<32:self.spd=prm
     elif prm>=32:self.bpm=prm;self._spt=self._gspt()
    elif eff==0xE:
     s2,a=prm>>4,prm&0xF
     if s2==1:   # E1x fine porta up
      if self.mod.linear:c.freq*=2.0**(a/768.0);c.tfreq=c.freq
      else:c.per=max(1,c.per-a);c.bper=c.per;c.freq=XM_APC/c.per;c.tfreq=c.freq
     elif s2==2:  # E2x fine porta down
      if self.mod.linear:c.freq*=2.0**(-a/768.0);c.tfreq=c.freq
      else:c.per+=a;c.bper=c.per;c.freq=XM_APC/c.per;c.tfreq=c.freq
     elif s2==6:  # E6x pattern loop
      if a==0:self._lsr=self.row
      elif self._lsc==0:self._lsc=a;self._pb=self._lsr;self._pj=self.op
      elif self._lsc>0:
       self._lsc-=1
       if self._lsc:self._pb=self._lsr;self._pj=self.op
     elif s2==0xA:c.vol=min(64,c.vol+a)
     elif s2==0xB:c.vol=max(0,c.vol-a)
     elif s2==0xC:c.prm=prm  # note cut tick a

   elif fmt=='IT':
    note,ins,vol,eff,prm=cell
    # IT note: 0xFF=no note, 0=C-0..119=B-9, 254=note cut, 255=note off
    if ins:
     if 1<=ins<=len(self.mod.ntbl):   # instrument mode
      sidx=0
      if note!=0xFF and note<=119:
       sidx=self.mod.ntbl[ins-1][note]  # ntbl indexed by note (0-based)
      elif c.snum:sidx=c.snum
      if sidx and sidx<len(self.mod.smp):
       c.snum=sidx;c.vol=self.mod.smp[sidx].vol
     elif ins<len(self.mod.smp):       # sample-only mode
      c.snum=ins;c.vol=self.mod.smp[ins].vol
    s=self.mod.smp[c.snum] if c.snum and c.snum<len(self.mod.smp) else None
    if note<=119:                      # 0-119 are valid notes (0=C-0)
     freq=_it_freq(note,s.c5 if s else 8363)
     if freq>0:
      if eff==7:c.ptgt=freq           # G = tone porta target
      else:self._trig(c,freq)
    elif note==254:c.on=False          # note cut
    elif note==255:pass                 # note off (simplified: ignore envelopes)
    if vol!=0xFF:
     if vol<=64:c.vol=vol
     elif 65<=vol<=74:c.vol=min(64,c.vol+(vol-65))   # fine vol up
     elif 75<=vol<=84:c.vol=max(0,c.vol-(vol-75))    # fine vol down
    c.eff=eff;c.prm=prm
    if eff==1:self.spd=max(1,prm)                     # A = speed
    elif eff==2:self._pj=prm%self.mod.sl              # B = jump
    elif eff==3:self._pb=(prm>>4)*10+(prm&0xF)        # C = break row
    elif eff==4:                                       # D = vol slide
     vh,vl=prm>>4,prm&0xF
     if prm>=0xF0:c.vol=max(0,c.vol-vl)
     elif vl==0xF:c.vol=min(64,c.vol+vh)
    elif eff==5 and prm:                               # E = porta down
     if prm&0xF0==0xF0 and self.mod.linear:           # EFx extra fine
      c.freq*=2.0**(-(prm&0xF)/768.0);c.tfreq=c.freq
     elif prm&0xF0==0xE0 and self.mod.linear:         # EEx fine
      c.freq*=2.0**(-(prm&0xF)*4/768.0);c.tfreq=c.freq
    elif eff==6 and prm:                               # F = porta up
     if prm&0xF0==0xF0 and self.mod.linear:
      c.freq*=2.0**((prm&0xF)/768.0);c.tfreq=c.freq
     elif prm&0xF0==0xE0 and self.mod.linear:
      c.freq*=2.0**((prm&0xF)*4/768.0);c.tfreq=c.freq
    elif eff==7 and prm:c.pspd=prm                    # G speed memory
    elif eff==8:                                       # H = vibrato
     if prm>>4:c.vs=prm>>4
     if prm&0xF:c.vd=prm&0xF
    elif eff==15 and prm:c.pos=prm*256.0              # O = sample offset
    elif eff==20:self.bpm=max(32,prm);self._spt=self._gspt()  # T = tempo
    elif eff==19:                                      # S = special
     s2,a=prm>>4,prm&0xF
     if s2==0xB:                                       # SBx = pattern loop
      if a==0:self._lsr=self.row
      elif self._lsc==0:self._lsc=a;self._pb=self._lsr;self._pj=self.op
      elif self._lsc>0:
       self._lsc-=1
       if self._lsc:self._pb=self._lsr;self._pj=self.op

 # ── tick effects (ticks 1..speed-1) ─────────────────────────────────────────

 def _tickfx(self):
  t=self.tick;fmt=self.mod.fmt;lin=self.mod.linear
  for c in self.ch:
   e,p=c.eff,c.prm

   if fmt=='MOD':
    if e==0 and p:                        # 0xy arpeggio
     sm=[0,p>>4,p&0xF][t%3]
     c.freq=_af(c.per)*(2.0**(sm/12.0)) if c.per else c.freq
    elif e==1:                            # 1xx porta up
     c.per=max(113,c.per-p);c.bper=c.per;c.freq=_af(c.per);c.tfreq=c.freq
    elif e==2:                            # 2xx porta down
     c.per+=p;c.bper=c.per;c.freq=_af(c.per);c.tfreq=c.freq
    elif e in(3,5):                       # 3xx/5xx tone porta (+vol slide)
     if c.ptgt and c.pspd:
      if c.per<c.ptgt:c.per=min(c.per+c.pspd,c.ptgt)
      elif c.per>c.ptgt:c.per=max(c.per-c.pspd,c.ptgt)
      c.freq=_af(c.per)
     if e==5:
      vh,vl=p>>4,p&0xF;c.vol=min(64,c.vol+vh) if vh else max(0,c.vol-vl)
    elif e in(4,6):                       # 4xx/6xx vibrato (+vol slide)
     vib=(SIN[c.vp&63]*c.vd)>>7
     c.freq=_af(max(1,c.bper-vib));c.vp=(c.vp+c.vs)&63
     if e==6:
      vh,vl=p>>4,p&0xF;c.vol=min(64,c.vol+vh) if vh else max(0,c.vol-vl)
    elif e==0xA:                          # Axx vol slide
     vh,vl=p>>4,p&0xF;c.vol=min(64,c.vol+vh) if vh else max(0,c.vol-vl)
    elif e==0xE:
     s2,a=p>>4,p&0xF
     if s2==0xC and t==a:c.vol=0          # ECx note cut
     elif s2==0xD and t==a:               # EDx note delay
      c.freq=_af(c.per) if c.per else c.freq;c.pos=0.0;c.on=c.snum>0

   elif fmt=='S3M':
    if e==4:                              # D = vol slide
     vh,vl=p>>4,p&0xF
     if p<0xF0 and vl!=0xF:c.vol=min(64,c.vol+vh) if vh else max(0,c.vol-vl)
    elif e==5 and p<0xE0:                 # E = porta down (normal)
     c.s3mper+=p*4
     if c.s3mper:c.freq=S3M_CLK/c.s3mper;c.tfreq=c.freq
    elif e==6 and p<0xE0:                 # F = porta up (normal)
     c.s3mper=max(1,c.s3mper-p*4)
     c.freq=S3M_CLK/c.s3mper;c.tfreq=c.freq
    elif e==7:                            # G = tone porta
     if c.pspd and c.s3mper and c.ptgt:
      if c.s3mper<c.ptgt:c.s3mper=min(c.s3mper+c.pspd*4,c.ptgt)
      elif c.s3mper>c.ptgt:c.s3mper=max(c.s3mper-c.pspd*4,c.ptgt)
      c.freq=S3M_CLK/c.s3mper;c.tfreq=c.freq
    elif e==8:                            # H = vibrato
     vib=(SIN[c.vp&63]*c.vd)>>7
     if c.s3mper:c.freq=S3M_CLK/max(1,c.s3mper-vib)
     c.vp=(c.vp+c.vs)&63

   elif fmt=='XM':
    if e==0 and p:                        # 0xy arpeggio
     sm=[0,p>>4,p&0xF][t%3]
     c.freq=c.tfreq*(2.0**(sm/12.0)) if c.tfreq>0 else c.freq
    elif e==1:                            # 1xx porta up
     if lin:c.freq*=2.0**(p/768.0);c.tfreq=c.freq
     else:c.per=max(1,c.per-p);c.bper=c.per;c.freq=XM_APC/c.per;c.tfreq=c.freq
    elif e==2:                            # 2xx porta down
     if lin:c.freq*=2.0**(-p/768.0);c.tfreq=c.freq
     else:c.per+=p;c.bper=c.per;c.freq=XM_APC/c.per;c.tfreq=c.freq
    elif e in(3,5):                       # 3xx/5xx tone porta
     if c.pspd:
      if lin:
       if c.ptgt:
        step=c.ptgt*(2.0**(c.pspd/768.0)-1.0)
        if c.freq<c.ptgt:c.freq=min(c.freq+step,c.ptgt)
        elif c.freq>c.ptgt:c.freq=max(c.freq-step,c.ptgt)
      else:
       if c.ptgt:
        if c.per<c.ptgt:c.per=min(c.per+c.pspd,c.ptgt)
        elif c.per>c.ptgt:c.per=max(c.per-c.pspd,c.ptgt)
        c.freq=XM_APC/max(1,c.per)
     if e==5:
      vh,vl=p>>4,p&0xF;c.vol=min(64,c.vol+vh) if vh else max(0,c.vol-vl)
    elif e in(4,6):                       # 4xx/6xx vibrato
     vib=(SIN[c.vp&63]*c.vd)>>7
     if lin:c.freq=c.tfreq*(2.0**(vib/1536.0))
     elif c.bper:c.freq=XM_APC/max(1,c.bper-vib)
     c.vp=(c.vp+c.vs)&63
     if e==6:
      vh,vl=p>>4,p&0xF;c.vol=min(64,c.vol+vh) if vh else max(0,c.vol-vl)
    elif e==0xA:
     vh,vl=p>>4,p&0xF;c.vol=min(64,c.vol+vh) if vh else max(0,c.vol-vl)
    elif e==0xE:
     s2,a=p>>4,p&0xF
     if s2==0xC and t==a:c.vol=0
     elif s2==0xD and t==a:c.pos=0.0;c.freq=c.tfreq;c.on=c.snum>0

   elif fmt=='IT':
    if e==4:                              # D = vol slide
     vh,vl=p>>4,p&0xF
     if p<0xF0 and vl!=0xF:c.vol=min(64,c.vol+vh) if vh else max(0,c.vol-vl)
    elif e==5 and p<0xE0 and lin:        # E = porta down
     c.freq*=2.0**(-p*4/768.0);c.tfreq=c.freq
    elif e==6 and p<0xE0 and lin:        # F = porta up
     c.freq*=2.0**(p*4/768.0);c.tfreq=c.freq
    elif e==7 and c.pspd and c.ptgt:     # G = tone porta
     step=c.ptgt*(2.0**(c.pspd*4/768.0)-1.0)
     if c.freq<c.ptgt:c.freq=min(c.freq+step,c.ptgt)
     elif c.freq>c.ptgt:c.freq=max(c.freq-step,c.ptgt)
    elif e==8:                            # H = vibrato
     vib=(SIN[c.vp&63]*c.vd)>>7
     c.freq=c.tfreq*(2.0**(vib/1536.0));c.vp=(c.vp+c.vs)&63
    elif e==19:
     s2,a=p>>4,p&0xF
     if s2==0xC and t==a:c.vol=0

 # ── sequencer ────────────────────────────────────────────────────────────────

 def _nrow(self):
  if self._pj>=0:
   self.op=self._pj
   self.row=max(0,self._pb) if self._pb>=0 else 0
   self._pj=self._pb=-1
  elif self._pb>=0:
   self.row=max(0,self._pb);self.op+=1;self._pb=-1
  else:
   self.row+=1
   if self.op<self.mod.sl:
    mr=len(self.mod.pats[self.mod.orders[self.op]])
    if self.row>=mr:self.row=0;self.op+=1
  if self.op>=self.mod.sl:self.ended=True

 def _atick(self):
  self.tick+=1
  if self.tick>=self.spd:
   self.tick=0;self._nrow();self._row0()
  else:
   self._tickfx()

 # ── audio generation ─────────────────────────────────────────────────────────

 def _gen_block(self,n):
  if self.ended:return np.zeros((n,2),dtype=np.float32)
  left=np.zeros(n,dtype=np.float32)
  right=np.zeros(n,dtype=np.float32)
  tp=self._tp;pos=0
  while pos<n:
   chunk=min(self._spt-tp,n-pos)
   if chunk<=0:tp=0;self._atick();continue
   for c in self.ch:
    buf=_mix(c,self.mod,chunk)
    if buf is not None:
     pan=c.pan/255.0
     lv=math.sqrt(max(0.0,1.0-pan));rv=math.sqrt(pan)
     left[pos:pos+chunk]+=buf*lv
     right[pos:pos+chunk]+=buf*rv
   pos+=chunk;tp+=chunk
   if tp>=self._spt:
    tp=0;self._atick()
    if self.ended:break
  self._tp=tp
  # scale: target RMS ~0.5 per channel, each channel contributes ~1/nc
  sc=1.0/max(1,self.nc//4)
  np.clip(left*sc,-1.0,1.0,out=left)
  np.clip(right*sc,-1.0,1.0,out=right)
  out=np.empty((n,2),dtype=np.float32)
  out[:,0]=left;out[:,1]=right
  return out

 def _worker(self):
  while self.playing and not self.ended:
   if self.paused:time.sleep(0.02);continue
   try:
    blk=self._gen_block(BLKSIZE)
    self._q.put(blk,timeout=1.0)
   except queue.Full:pass
   except Exception as e:
    import traceback;traceback.print_exc();break

 def _cb(self,out,frames,ti,st):
  if not self.playing or self.paused:out.fill(0);return
  try:
   blk=self._q.get_nowait()
   n=min(frames,len(blk));out[:n]=blk[:n]
   if n<frames:out[n:].fill(0)
  except queue.Empty:out.fill(0)

 def start(self):
  self.playing=True;self.paused=self.ended=False
  self._row0()
  self._wt=threading.Thread(target=self._worker,daemon=True)
  self._wt.start()
  self._st=sd.OutputStream(samplerate=SR,channels=2,dtype='float32',
                            blocksize=BLKSIZE,callback=self._cb)
  self._st.start()

 def stop(self):
  self.playing=False
  if self._st:
   try:self._st.stop();self._st.close()
   except:pass
   self._st=None

 def restart(self):
  with self._lk:
   was=self.playing;self.stop()
   self.op=self.row=self.tick=self._tp=0
   self._pb=self._pj=-1;self._lsr=self._lsc=0
   self.spd=self.mod.spd;self.bpm=self.mod.bpm
   self._spt=self._gspt();self.ended=False
   for c in self.ch:c.__init__()
   self._ipan()
   while not self._q.empty():
    try:self._q.get_nowait()
    except:pass
  if was:self.start()

 def toggle_pause(self):self.paused=not self.paused

 @property
 def stat(self):
  col='\033[33m' if self.paused else '\033[35m' if self.ended else '\033[32m'
  tag='PAUSED' if self.paused else 'ENDED ' if self.ended else 'PLAY  '
  op=min(self.op,self.mod.sl-1)
  return(f"{col}{tag}\033[0m  ord:{self.op:02d}/{self.mod.sl-1:02d}"
         f"  pat:{self.mod.orders[op]:03d}  row:{self.row:03d}"
         f"  spd:{self.spd}  bpm:{self.bpm}")

# ── file browsing ─────────────────────────────────────────────────────────────

def find_files(path):
 p=Path(path)
 if p.is_file() and p.suffix.lower() in EXTS:return [str(p)]
 if p.is_dir():
  r=[]
  for e in EXTS:r.extend(str(f) for f in sorted(p.rglob(f'*{e}')))
  return sorted(set(r))
 import glob as _g
 return [f for f in _g.glob(path,recursive=True) if Path(f).suffix.lower() in EXTS]

def pick(files):
 if not files:return None
 if len(files)==1:return files[0]
 raw_off();sys.stdout.write('\033[2J\033[H')
 for i,f in enumerate(files[:50]):
  print(f"  \033[36m{i+1:2d}\033[0m  {Path(f).name}")
 if len(files)>50:print(f"  ...{len(files)-50} more")
 try:n=int(input('\n  # ').strip() or '1')-1
 except:n=0
 raw_on()
 return files[n] if 0<=n<len(files) else files[0]

def load_play(path,cur):
 files=find_files(path)
 if not files:return cur,f"nothing found: {path}"
 chosen=pick(files)
 if not chosen:return cur,'cancelled'
 try:
  p=Player(load(chosen));p._fp=chosen
  if cur:cur.stop()
  p.start();return p,''
 except Exception as e:
  import traceback;traceback.print_exc()
  return cur,f"error: {e}"

def prompt_load(cur):
 raw_off();sys.stdout.write('\033[2J\033[H')
 try:raw=input('  path (enter=here, or paste/drag): ').strip()
 except EOFError:raw=''
 raw_on()
 return load_play(raw.strip('"').strip("'") or '.',cur)

# ── UI ────────────────────────────────────────────────────────────────────────

G='\033[1;32m';D='\033[90m';R='\033[0m';Y='\033[33m';C='\033[36m'

def render(pl,msg=''):
 out='\033[H'
 out+=(f"{G}MrB-ModPlay{R} {D}|{R} {D}MOD S3M XM IT{R}  "
       f"{D}P=load  S=stop  SPC=pause  R=restart  Q=quit{R}\033[K\r\n")
 out+=f"{D}{'-'*66}{R}\033[K\r\n"
 if pl:
  m=pl.mod;nm=Path(pl._fp).name if hasattr(pl,'_fp') else '?'
  fmode=('amiga','linear')[m.linear]
  out+=(f"  {G}{nm}{R}  "
        f"{D}{m.title or '(untitled)'}  "
        f"[{C}{m.fmt}{D}  {m.nc}ch  {len(m.smp)-1}smp  {fmode}]{R}\033[K\r\n")
  out+=f"  {pl.stat}\033[K\r\n"
 else:
  out+=f"  {D}no module loaded -- press P to load{R}\033[K\r\n"
 if msg:out+=f"  {Y}>> {msg}{R}\033[K\r\n"
 out+='\033[J'
 sys.stdout.write(out);sys.stdout.flush()

def run(pl=None,msg=''):
 sys.stdout.write('\033[2J');raw_on()
 try:
  while True:
   render(pl,msg);msg='';time.sleep(0.12)
   if not kbhit():continue
   k=getch()
   if k.lower()=='q':break
   elif k.lower()=='p':
    pl,msg=prompt_load(pl);sys.stdout.write('\033[2J')
    msg=msg or(f"playing {Path(pl._fp).name}" if pl else 'no file')
   elif k.lower()=='s':
    if pl:pl.stop();msg='stopped'
   elif k==' ':
    if pl and pl.playing:pl.toggle_pause();msg='paused' if pl.paused else 'resumed'
   elif k.lower()=='r':
    if pl:pl.restart();msg='restarted'
 finally:
  if pl:pl.stop()
  raw_off()
  sys.stdout.write('\033[2J\033[H');print(f'{G}bye{R}\n')

if __name__=='__main__':
 pl,msg=None,''
 if len(sys.argv)>1:
  arg=' '.join(sys.argv[1:]).strip('"').strip("'")
  pl,msg=load_play(arg,None)
 run(pl,msg)
