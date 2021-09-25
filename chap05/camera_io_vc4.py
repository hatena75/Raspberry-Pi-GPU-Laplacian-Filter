#coding:utf-8
import sys
import io
import numpy as np
from PIL import Image, ImageFilter

from videocore.assembler import qpu
from videocore.driver import Driver

from time import sleep, clock_gettime, CLOCK_MONOTONIC
from picamera import PiCamera
import picamera.array


sys.path.append("../00_utils/")
import hdmi
import camera
from fps import FPS

def setCamera(w, h):
  camera = PiCamera()
  camera.resolution = (w, h)

  return camera

def mask(idx):
    values = [1]*16
    values[idx] = 0
    return values

@qpu
def piadd(asm):
    IN_ADDR   = 0 #インデックス
    OUT_ADDR  = 1
    IO_ITER   = 2
    THR_ID    = 3
    THR_NM    = 4
    COMPLETED = 0 #セマフォ用

    
    ldi(null,mask(IN_ADDR),set_flags=True)  # r2にuniformを格納
    mov(r2,uniform,cond='zs')
    ldi(null,mask(OUT_ADDR),set_flags=True)
    mov(r2,uniform,cond='zs')
    ldi(null,mask(IO_ITER),set_flags=True)
    mov(r2,uniform,cond='zs')
    ldi(null,mask(THR_ID),set_flags=True)
    mov(r2,uniform,cond='zs')
    ldi(null,mask(THR_NM),set_flags=True)
    mov(r2,uniform,cond='zs')

    imul24(r3,element_number,4) 
    rotate(broadcast,r2,-IN_ADDR)
    iadd(r0,r5,r3) # r0:IN_ADDR(連番)

    L.loop

    ldi(r1, 16*4*2*30)  # 上下のアドレス分
    ldi(broadcast,16*4) #r5 == 16*4

    for i in range(30):
        #ra
        mov(tmu0_s,r0)
        nop(sig='load tmu0')
        # 中心-4
        fsub(ra[i], 0.0, r4)
        nop()
        fsub(ra[i], ra[i], r4)
        nop()
        fsub(ra[i], ra[i], r4)
        nop()
        fsub(ra[i], ra[i], r4)
        # 左右+1
        rotate(r3, r4, 1)
        fadd(ra[i], ra[i], r3)
        rotate(r3, r4, -1)
        fadd(ra[i], ra[i], r3)
        # 上下+1
        iadd(r3, r0, r1) #下のアドレス
        mov(tmu0_s,r3)
        nop(sig='load tmu0') # r4 = 下の値
        fadd(ra[i], ra[i], r4)
        isub(r3, r0, r1) #上のアドレス
        mov(tmu0_s,r3)
        nop(sig='load tmu0') # r4 = 上の値
        fadd(ra[i], ra[i], r4)
        iadd(r0, r0, r5)

        #rb
        mov(tmu1_s,r0)
        nop(sig='load tmu1')
        # 中心-4
        fsub(rb[i], 0.0, r4)
        nop()
        fsub(rb[i], rb[i], r4)
        nop()
        fsub(rb[i], rb[i], r4)
        nop()
        fsub(rb[i], rb[i], r4)
        # 左右+1
        rotate(r3, r4, 1)
        fadd(rb[i], rb[i], r3)
        rotate(r3, r4, -1)
        fadd(rb[i], rb[i], r3)
        # 上下+1
        iadd(r3, r0, r1) #下のアドレス
        mov(tmu1_s,r3)
        nop(sig='load tmu1') # r4 = 下の値
        fadd(rb[i], rb[i], r4)
        isub(r3, r0, r1) #上のアドレス
        mov(tmu1_s,r3)
        nop(sig='load tmu1') # r4 = 上の値
        fadd(rb[i], rb[i], r4)
        iadd(r0, r0, r5)

    ldi(r3,60*16*4)

    mutex_acquire()
    rotate(broadcast,r2,-OUT_ADDR)
    setup_vpm_write(mode='32bit horizontal',Y=0,X=0)

    for i in range(30):
        mov(vpm,ra[i])
        mov(vpm,rb[i])

    setup_dma_store(mode='32bit horizontal',Y=0,nrows=60)
    start_dma_store(r5)
    wait_dma_store()

    mutex_release()

    ldi(null,mask(IO_ITER),set_flags=True)
    isub(r2,r2,1,cond='zs')
    jzc(L.loop)
    ldi(null,mask(OUT_ADDR),set_flags=True)
    iadd(r2,r2,r3,cond='zs')
    nop()



#====semaphore=====    
    sema_up(COMPLETED)
    rotate(broadcast,r2,-THR_ID)
    iadd(null,r5,-1,set_flags=True)
    jzc(L.skip_fin)
    nop()
    nop()
    nop()
    rotate(broadcast,r2,-THR_NM)
    iadd(r0, r5, -1,set_flags=True)
    L.sem_down
    jzc(L.sem_down)
    sema_down(COMPLETED)    # すべてのスレッドが終了するまで待つ
    nop()
    iadd(r0, r0, -1)
    
    interrupt()
    
    L.skip_fin
    
    exit(interrupt=False)

    
with Driver() as drv:
    
    DISPLAY_W, DISPLAY_H = hdmi.getResolution()

    # 画像サイズ
    H=360
    W=320
    
    # cameraセットアップ
    cam = camera.setCamera(320, 368)
    cam.framerate = 30
    overlay_dstimg = camera.PiCameraOverlay(cam, 3)
    cam.start_preview(fullscreen=False, window=(0, 0, W*2, H*2))

    # 画面のクリア
    back_img = Image.new('RGBA', (DISPLAY_W, DISPLAY_H), 0)
    hdmi.printImg(back_img, *hdmi.getResolution(), hdmi.PUT)

    n_threads=12
    SIMD=16
    R=60

    th_H    = int(H/n_threads) #1スレッドの担当行
    th_ele  = th_H*W #1スレッドの担当要素
    io_iter = int(th_ele/(R*SIMD)) #何回転送するか

    print(th_H, th_ele, io_iter)

    IN  = drv.alloc((H,W),'float32')
    OUT = drv.alloc((H,W),'float32')
    OUT[:] = 0.0

    uniforms=drv.alloc((n_threads,5),'uint32')
    for th in range(n_threads):
        uniforms[th,0]=IN.addresses()[int(th_H*th),0]
        uniforms[th,1]=OUT.addresses()[int(th_H*th),0]
    uniforms[:,2]=int(io_iter)
    uniforms[:,3]=np.arange(1,(n_threads+1))
    uniforms[:,4]=n_threads

    code=drv.program(piadd)

    try:
      fps = FPS()
      stream = io.BytesIO()
      while True:
          input_img_RGB = camera.capture2PIL(cam, stream, (320, 368))
          input_img = input_img_RGB.convert('L')
          pil_img = input_img.resize((W, H))

          IN[:] = np.asarray(pil_img)[:]
          CC = IN

          drv.execute(
              n_threads= n_threads,
              program  = code,
              uniforms = uniforms
          )

          out_img = Image.fromarray(OUT.astype(np.uint8))
          draw_img = Image.new('L', (W*2, H), 0)
          draw_img.paste(out_img, (0, 0))
          hdmi.addText(draw_img, *(W+10, 32 * 0), "Raspberry Pi VC4")
          hdmi.addText(draw_img, *(W+10, 32 * 2), f'Binarization')
          hdmi.addText(draw_img, *(W+10, 32 * 3), f'{H}x{W}')
          hdmi.addText(draw_img, *(W+10, 32 * 5), f'{fps.update():.3f} FPS')

          draw_img = draw_img.convert('RGB')
          overlay_dstimg.OnOverlayUpdated(draw_img, format='rgb', fullscreen=False, window=(W*2, 0, W*4, H*2))

          print(f'{fps.get():.3f} FPS')

    except KeyboardInterrupt:
      # Ctrl-C を捕まえた！
      print('\nCtrl-C is pressed, end of execution!')
      cam.stop_preview()
      cam.close()
