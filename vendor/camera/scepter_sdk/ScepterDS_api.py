from .ScepterDS_define import *
import platform
import os
import ctypes
import sys
from ctypes import *
import atexit
from typing import Optional

gCallbackFuncList=[]
system_ = platform.system().lower()
machine_ = platform.machine().lower()
architecture_ = platform.architecture()[0]

# SDK 所在目录（基于 __file__ 定位，确保打包后也能正确加载库）
_SDK_DIR = os.path.dirname(os.path.abspath(__file__))


def _linux_lib_dir() -> str:
    """Linux 库目录：linux/x64（x86_64）或 linux/aarch64（ARM64，DEC-078）。"""
    low = machine_.lower()
    if low in ("x86_64", "amd64"):
        return os.path.join(_SDK_DIR, "linux", "x64")
    if low in ("aarch64", "arm64"):
        return os.path.join(_SDK_DIR, "linux", "aarch64")
    raise ImportError(f"ScepterSDK 不支持的 Linux 架构: {machine_}")


if system_ == 'linux':
    _lib_dir = _linux_lib_dir()
    # 主库 RUNPATH 已含 $ORIGIN；预加载依赖库做双保险（冻结布局丢失 RUNPATH 时也能用）
    _dep = os.path.join(_lib_dir, "libDSImgPreProcess.so")
    if os.path.isfile(_dep):
        try:
            CDLL(_dep, mode=RTLD_GLOBAL)
        except OSError as exc:
            raise ImportError(f"ScepterSDK 依赖库加载失败 {_dep}: {exc}") from exc
    libpath = os.path.join(_lib_dir, "libScepter_api.so")
    if not os.path.isfile(libpath):
        raise ImportError(f"ScepterSDK Linux 库缺失: {libpath}")
    sc_cam_lib = cdll.LoadLibrary(libpath)
elif system_ == 'windows':
    if machine_ == 'amd64' and architecture_ == '64bit':
        _dll_dir = os.path.join(_SDK_DIR, "windows", "x64")
        # 将 DLL 目录加入 PATH，确保 Scepter_api.dll 能找到 Drivers 下的依赖 DLL
        _dll_dir_abs = os.path.abspath(_dll_dir)
        if _dll_dir_abs not in os.environ.get("PATH", ""):
            os.environ["PATH"] = _dll_dir_abs + os.pathsep + os.environ.get("PATH", "")
        libpath = os.path.join(_dll_dir, "Scepter_api.dll")
        sc_cam_lib = cdll.LoadLibrary(libpath)
    else:
        raise ImportError("ScepterSDK 仅支持 Windows x64 平台")
else:
    raise ImportError(f"不支持的操作系统: {system_}")


# --------------------------
# GlobalManager
# --------------------------
class _CameraSDKGlobalManager:
    _instance: Optional["_CameraSDKGlobalManager"] = None
    _ref_count: int = 0
    _initialized: bool = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            atexit.register(cls._instance._safe_shutdown)
        return cls._instance

    def initialize(self) -> bool:
        if not self._initialized:
            ret = sc_cam_lib.scInitialize()
            if ret == 0:
                self._initialized = True
                print("scInitialize ok.")
                return True
            else:
                raise RuntimeError("scInitialize: {ret}")
        return True
    def shutdown(self) -> bool:
        if self._initialized and self._ref_count == 0:
            ret = sc_cam_lib.scShutdown()
            if ret == 0:
                self._initialized = False
                print("scShutdown ok")
                return True
            else:
                print("scShutdown error: {ret}")
        return False
    def add_ref(self):
        self._ref_count += 1
        self.initialize()
        
    def release_ref(self):
        if self._ref_count > 0:
            self._ref_count -= 1
        self.shutdown()

    def _safe_shutdown(self):
        if self._initialized:
            ret = sc_cam_lib.scShutdown()
            if ret == 0:
                self._initialized = False
                print("scShutdown ok.")
            else:
                print("scShutdown error: {ret}")

# --------------------------
# ScepterTofCam
# --------------------------
class ScepterTofCam():
    device_handle = c_void_p(0)
    def __init__(self):       
        self.device_handle = c_void_p(0)
        self._global_manager = _CameraSDKGlobalManager()
        self._global_manager.add_ref()
        
    def __del__(self):
        self._global_manager.release_ref()
  
    def scGetSDKVersion(self): 
        tmp = c_char * 64
        version = tmp()
        return sc_cam_lib.scGetSDKVersion(version, 63),version.value

    def scGetDeviceCount(self, scanTime = c_uint32(33)):
        count = c_int()
        sc_cam_lib.scGetDeviceCount(byref(count), scanTime)
        return count.value
    
    def scGetDeviceInfoList(self, cam_count = 1):
        tmp  = ScDeviceInfo* cam_count
        device_infolist = tmp() 
        return sc_cam_lib.scGetDeviceInfoList(cam_count, device_infolist),device_infolist

    def scOpenDeviceBySN(self,  SN=c_char_p()):
        if SN:
            return sc_cam_lib.scOpenDeviceBySN(SN, byref(self.device_handle))
        else:
            return ScReturnStatus.SC_INPUT_POINTER_IS_NULL
    
    def scOpenDeviceByIP(self,  ip=c_char_p()):
        if ip:
            return sc_cam_lib.scOpenDeviceByIP(ip, byref(self.device_handle))
        else:
            return ScReturnStatus.SC_INPUT_POINTER_IS_NULL, 0
        
    def scCloseDevice(self):
        return sc_cam_lib.scCloseDevice(byref(self.device_handle))

    def scStartStream(self):
        return sc_cam_lib.scStartStream(self.device_handle)

    def scStopStream(self):
        return sc_cam_lib.scStopStream(self.device_handle)

    def scGetFrameReady(self,waitTime = c_uint16(33)):
        frameready = ScFrameReady()
        if not self.device_handle:
            return -3, frameready
        return sc_cam_lib.scGetFrameReady(self.device_handle, waitTime, byref(frameready)), frameready

    def scGetFrame(self,  frametype = ScFrameType.SC_DEPTH_FRAME):   
        Scframe = ScFrame() 
        return sc_cam_lib.scGetFrame(self.device_handle, frametype.value, byref(Scframe)), Scframe

    def scGetDepthRangeValue(self):
        minValue = c_int16(0)
        maxValue = c_int16(0)
        return sc_cam_lib.scGetDepthRangeValue(self.device_handle, byref(minValue), byref(maxValue)), minValue.value, maxValue.value
    
    def scGetSensorIntrinsicParameters(self, sensorType = ScSensorType.SC_TOF_SENSOR):
        CameraParameters = ScSensorIntrinsicParameters()
        return sc_cam_lib.scGetSensorIntrinsicParameters(self.device_handle, sensorType.value, byref(CameraParameters)), CameraParameters

    def scGetSensorExtrinsicParameters(self):
        CameraExtrinsicParameters = ScSensorExtrinsicParameters()
        return sc_cam_lib.scGetSensorExtrinsicParameters(self.device_handle, byref(CameraExtrinsicParameters)), CameraExtrinsicParameters
    
    def scGetFirmwareVersion(self): 
        tmp = c_char * 64
        fw = tmp()
        return sc_cam_lib.scGetFirmwareVersion(self.device_handle, fw, 63),fw.value

    def scGetDeviceMACAddress(self):
        tmp = c_char * 18
        mac = tmp()
        return sc_cam_lib.scGetDeviceMACAddress(self.device_handle, mac), mac.value

    def scSetDeviceDHCPEnabled(self, enable = c_bool(True)):
        return sc_cam_lib.scSetDeviceDHCPEnabled(self.device_handle, enable)

    def scGetDeviceDHCPEnabled(self):
        enable = c_bool(True)
        return sc_cam_lib.scGetDeviceDHCPEnabled(self.device_handle, byref(enable)), enable.value

    def scSetDeviceIPAddr(self, IPAddr=c_char_p(), length=c_int32(16)):
        return sc_cam_lib.scSetDeviceIPAddr(self.device_handle, IPAddr, length)

    def scGetDeviceIPAddr(self):
        tmp = c_char * 16
        IPAddr = tmp()
        return sc_cam_lib.scGetDeviceIPAddr(self.device_handle, IPAddr), IPAddr.value

    def scSetDeviceSubnetMask(self, subnetMask=c_char_p(), length=c_int32(16)):
        return sc_cam_lib.scSetDeviceSubnetMask(self.device_handle, subnetMask, length)

    def scGetDeviceSubnetMask(self):
        tmp = c_char * 16
        subnetMask = tmp()
        return sc_cam_lib.scGetDeviceSubnetMask(self.device_handle, subnetMask), subnetMask.value

    def scSetRealTimeSyncConfig(self, params = ScTimeSyncConfig()):
        return sc_cam_lib.scSetRealTimeSyncConfig(self.device_handle, params)

    def scGetRealTimeSyncConfig(self):
        pParams = ScTimeSyncConfig()
        return sc_cam_lib.scGetRealTimeSyncConfig(self.device_handle, byref(pParams)),pParams

    def scSetFrameRate(self, value = c_uint8(30)):
        return sc_cam_lib.scSetFrameRate(self.device_handle, value) 

    def scGetFrameRate(self):
        rate = c_uint8(1)
        return sc_cam_lib.scGetFrameRate(self.device_handle, byref(rate)), rate.value

    def scSetWorkMode(self,  mode = ScWorkMode.SC_ACTIVE_MODE):
        return sc_cam_lib.scSetWorkMode(self.device_handle, mode.value) 

    def scGetWorkMode(self):
        mode = c_uint8(0)
        return sc_cam_lib.scGetWorkMode(self.device_handle, byref(mode)), mode.value

    def scSetSoftwareTriggerParameter(self, param = c_uint8(0)):
        return sc_cam_lib.scSetSoftwareTriggerParameter(self.device_handle, param)

    def scGetSoftwareTriggerParameter(self):
        param = c_uint8(0)
        return sc_cam_lib.scGetSoftwareTriggerParameter(self.device_handle, byref(param)), param.value

    def scSoftwareTriggerOnce(self):
        return sc_cam_lib.scSoftwareTriggerOnce(self.device_handle)
   
    def scSetInputSignalParamsForHWTrigger(self, params = ScInputSignalParamsForHWTrigger()):
        return sc_cam_lib.scSetInputSignalParamsForHWTrigger(self.device_handle, params)

    def scGetInputSignalParamsForHWTrigger(self):
        params = ScInputSignalParamsForHWTrigger()
        return sc_cam_lib.scGetInputSignalParamsForHWTrigger(self.device_handle, byref(params)), params

    def scSetIRGMMGain(self, gmmgain = c_uint8(20)):
        return sc_cam_lib.scSetIRGMMGain(self.device_handle, gmmgain) 

    def scGetIRGMMGain(self):
        gmmgain = c_uint8(1)
        return sc_cam_lib.scGetIRGMMGain(self.device_handle, byref(gmmgain)), gmmgain.value

    def scSetIRGMMCorrection(self, params = ScIRGMMCorrectionParams()):
        return sc_cam_lib.scSetIRGMMCorrection(self.device_handle, params)

    def scGetIRGMMCorrection(self):
        params = ScIRGMMCorrectionParams()
        return sc_cam_lib.scGetIRGMMCorrection(self.device_handle, byref(params)), params

    def scSetColorPixelFormat(self,pixelFormat=ScPixelFormat.SC_PIXEL_FORMAT_BGR_888_JPEG):
        return sc_cam_lib.scSetColorPixelFormat(self.device_handle, pixelFormat.value)

    def scSetColorGain(self, params = c_float(1.0)):
        return sc_cam_lib.scSetColorGain(self.device_handle,  params)

    def scGetColorGain(self):
        tmp = c_float*1
        params = tmp()
        return sc_cam_lib.scGetColorGain(self.device_handle,  params), params[0]

    def scGetSupportedResolutionList(self, type = ScSensorType.SC_TOF_SENSOR, cam_count=1):
        tmp = ScResolutionList * cam_count
        pList = tmp()
        return sc_cam_lib.scGetSupportedResolutionList(self.device_handle, type.value, byref(pList)), pList

    def scSetColorResolution(self, w = c_int32(1600), h = c_int32(1200)):
        return sc_cam_lib.scSetColorResolution(self.device_handle, w, h)

    def scGetColorResolution(self):
        w = c_int32(1600)
        h = c_int32(1200)
        return sc_cam_lib.scGetColorResolution(self.device_handle, byref(w), byref(h)), w.value, h.value

    def scSetToFResolution(self, w = c_int32(1600), h = c_int32(1200)):
        return sc_cam_lib.scSetToFResolution(self.device_handle, w, h)

    def scGetToFResolution(self):
        w = c_int32(1600)
        h = c_int32(1200)
        return sc_cam_lib.scGetToFResolution(self.device_handle, byref(w), byref(h)), w.value, h.value

    def scSetExposureControlMode(self, sensorType = ScSensorType.SC_TOF_SENSOR, mode = ScExposureControlMode.SC_EXPOSURE_CONTROL_MODE_MANUAL):
        return sc_cam_lib.scSetExposureControlMode(self.device_handle, sensorType.value, mode.value) 

    def scGetExposureControlMode(self, sensorType = ScSensorType.SC_TOF_SENSOR):
        mode = c_uint8(1)
        return sc_cam_lib.scGetExposureControlMode(self.device_handle, sensorType.value, byref(mode)), mode.value

    def scSetExposureTime(self, sensorType = ScSensorType.SC_TOF_SENSOR, params = c_int32(0)):
        return sc_cam_lib.scSetExposureTime(self.device_handle, sensorType.value, params)

    def scGetExposureTime(self, sensorType = ScSensorType.SC_TOF_SENSOR):
        params = c_int32(0)
        return sc_cam_lib.scGetExposureTime(self.device_handle, sensorType.value, byref(params)), params.value

    def scSetColorAECMaxExposureTime(self, params=c_int32(0)):
        return sc_cam_lib.scSetColorAECMaxExposureTime(self.device_handle, params)

    def scGetColorAECMaxExposureTime(self):
        params = c_int32(0)
        return sc_cam_lib.scGetColorAECMaxExposureTime(self.device_handle, byref(params)), params.value

    def scSetColorAECROI(self,  x = c_uint16(0), y = c_uint16(0), width = c_uint16(640), height =c_uint16(480)):
        return sc_cam_lib.scSetColorAECROI(self.device_handle, x, y, width, height)

    def scGetColorAECROI(self):
        x = c_uint16(0)
        y = c_uint16(0)
        width =c_uint16(0)        
        height =c_uint16(0)
        return sc_cam_lib.scGetColorAECROI(self.device_handle, byref(x), byref(y), byref(width), byref(height)), x.value, y.value, width.value, height.value

    def scGetMaxExposureTime(self, sensorType = ScSensorType.SC_COLOR_SENSOR):
        tmp = c_int32(1000)
        return sc_cam_lib.scGetMaxExposureTime(self.device_handle, sensorType.value, byref(tmp)), tmp.value

    def scSetHDRModeEnabled(self, enable =  c_bool(True)):
        return sc_cam_lib.scSetHDRModeEnabled(self.device_handle, enable)

    def scGetHDRModeEnabled(self):
        enable = c_bool(True)
        return sc_cam_lib.scGetHDRModeEnabled(self.device_handle, byref(enable)), enable.value

    def scGetFrameCountOfHDRMode(self):
        pCount = c_int32(0)
        return sc_cam_lib.scGetFrameCountOfHDRMode(self.device_handle, byref(pCount)), pCount.value

    def scSetExposureTimeOfHDR(self, frameIndex = c_uint8(0), exposureTime = c_int32(0)):
        return sc_cam_lib.scSetExposureTimeOfHDR(self.device_handle, frameIndex, exposureTime)

    def scGetExposureTimeOfHDR(self, frameIndex = c_uint8(0)):
        exposureTime = c_int32(0)
        return sc_cam_lib.scGetExposureTimeOfHDR(self.device_handle, frameIndex, byref(exposureTime)), exposureTime.value

    def scGetMaxExposureTimeOfHDR(self, frameIndex = c_uint8(0)):
        exposureTime = c_int32(0)
        return sc_cam_lib.scGetMaxExposureTimeOfHDR(self.device_handle, frameIndex, byref(exposureTime)), exposureTime.value

    def scSetWDRModeEnabled(self, enable =  c_bool(True)):
        return sc_cam_lib.scSetWDRModeEnabled(self.device_handle, enable)

    def scGetWDRModeEnabled(self):
        enable = c_bool(True)
        return sc_cam_lib.scGetWDRModeEnabled(self.device_handle, byref(enable)), enable.value

    def scGetFrameCountOfWDRMode(self):
        pCount = c_int32(0)
        return sc_cam_lib.scGetFrameCountOfWDRMode(self.device_handle, byref(pCount)), pCount.value

    def scSetExposureTimeOfWDR(self, frameIndex = c_uint8(0), exposureTime = c_int32(0)):
        return sc_cam_lib.scSetExposureTimeOfWDR(self.device_handle, frameIndex, exposureTime)

    def scGetExposureTimeOfWDR(self, frameIndex = c_uint8(0)):
        exposureTime = c_int32(0)
        return sc_cam_lib.scGetExposureTimeOfWDR(self.device_handle, frameIndex, byref(exposureTime)), exposureTime.value

    def scGetMaxExposureTimeOfWDR(self, frameIndex = c_uint8(0)):
        exposureTime = c_int32(0)
        return sc_cam_lib.scGetMaxExposureTimeOfWDR(self.device_handle, frameIndex, byref(exposureTime)), exposureTime.value

    def scSetTimeFilterParams(self, params = ScTimeFilterParams()): 
        return sc_cam_lib.scSetTimeFilterParams(self.device_handle, params)
    
    def scGetTimeFilterParams(self): 
        params = ScTimeFilterParams()
        return sc_cam_lib.scGetTimeFilterParams(self.device_handle, byref(params)),params

    def scSetConfidenceFilterParams(self, params = ScConfidenceFilterParams()): 
        return sc_cam_lib.scSetConfidenceFilterParams(self.device_handle, params)
    
    def scGetConfidenceFilterParams(self): 
        params = ScConfidenceFilterParams()
        return sc_cam_lib.scGetConfidenceFilterParams(self.device_handle, byref(params)),params
    
    def scSetFlyingPixelFilterParams(self, params = ScFlyingPixelFilterParams()): 
        return sc_cam_lib.scSetFlyingPixelFilterParams(self.device_handle, params)
    
    def scGetFlyingPixelFilterParams(self): 
        params = ScFlyingPixelFilterParams()
        return sc_cam_lib.scGetFlyingPixelFilterParams(self.device_handle, byref(params)),params

    def scSetFillHoleFilterEnabled(self, enable = c_bool(True)): 
        return sc_cam_lib.scSetFillHoleFilterEnabled(self.device_handle, enable)
    
    def scGetFillHoleFilterEnabled(self): 
        enable = c_bool(True)
        return sc_cam_lib.scGetFillHoleFilterEnabled(self.device_handle, byref(enable)),enable.value

    def scSetSpatialFilterEnabled(self, enable = c_bool(True)): 
        return sc_cam_lib.scSetSpatialFilterEnabled(self.device_handle, enable)
    
    def scGetSpatialFilterEnabled(self): 
        enable = c_bool(True)
        return sc_cam_lib.scGetSpatialFilterEnabled(self.device_handle, byref(enable)),enable.value

    def scSetTransformColorImgToDepthSensorEnabled(self, enabled = c_bool(True)): 
        return sc_cam_lib.scSetTransformColorImgToDepthSensorEnabled(self.device_handle,  enabled)
    
    def scGetTransformColorImgToDepthSensorEnabled(self): 
        enabled = c_bool(True)
        return sc_cam_lib.scGetTransformColorImgToDepthSensorEnabled(self.device_handle,  byref(enabled)),enabled.value

    def scSetTransformDepthImgToColorSensorEnabled(self, enabled = c_bool(True)): 
        return sc_cam_lib.scSetTransformDepthImgToColorSensorEnabled(self.device_handle,  enabled)
    
    def scGetTransformDepthImgToColorSensorEnabled(self): 
        enabled = c_bool(True)
        return sc_cam_lib.scGetTransformDepthImgToColorSensorEnabled(self.device_handle,  byref(enabled)),enabled.value

    def scTransformDepthPointToColorPoint(self, depthPoint = ScDepthVector3(), colorSize = ScVector2u16()):
        pPointInColor = ScVector2u16()
        return sc_cam_lib.scTransformDepthPointToColorPoint(self.device_handle, depthPoint, colorSize, byref(pPointInColor)), pPointInColor

    def scConvertDepthToPointCloud(self, pDepthVector = ScDepthVector3(), pointCount = c_int32(0), pSensorParam = ScSensorIntrinsicParameters()):
        tmp = ScVector3f * pointCount
        pWorldVector = tmp()
        return sc_cam_lib.scConvertDepthToPointCloud(self.device_handle, byref(pDepthVector), byref(pWorldVector), pointCount, byref(pSensorParam)), pWorldVector

    def scConvertDepthFrameToPointCloudVector(self, depthFrame = ScFrame()): 
        len = depthFrame.width*depthFrame.height
        tmp =ScVector3f*len
        pointlist = tmp()
        return sc_cam_lib.scConvertDepthFrameToPointCloudVector(self.device_handle, byref(depthFrame), byref(pointlist)), pointlist

    def scSetParamsByJson(self, pfilePath):
        path = (c_char * 1000)(*bytes(pfilePath, 'utf-8'))
        return sc_cam_lib.scSetParamsByJson(self.device_handle,  byref(path))

    def scExportParamInitFile(self, pfilePath):
        path = (c_char * 1000)(*bytes(pfilePath, 'utf-8'))
        return sc_cam_lib.scExportParamInitFile(self.device_handle, byref(path))

    def scImportParamInitFile(self, pfilePath):
        path = (c_char * 1000)(*bytes(pfilePath, 'utf-8'))
        return sc_cam_lib.scImportParamInitFile(self.device_handle, byref(path))

    def scRestoreParamInitFile(self):
        return sc_cam_lib.scRestoreParamInitFile(self.device_handle)

    def scRebootDevie(self):
        return sc_cam_lib.scRebootDevie(self.device_handle)

    def scSetHotPlugStatusCallback(self,callbackfunc= c_void_p): 
        callbackFunc_= ctypes.CFUNCTYPE(c_void_p,POINTER(ScDeviceInfo),c_int32)(callbackfunc)    
        gCallbackFuncList.append(callbackFunc_)
        return sc_cam_lib.scSetHotPlugStatusCallback(callbackFunc_)

    def scStartUpgradeFirmWare(self, pfilePath):
        path = (c_char * 1000)(*bytes(pfilePath, 'utf-8'))
        return sc_cam_lib.scStartUpgradeFirmWare(self.device_handle,  byref(path))

    def scGetUpgradeStatus(self):
        pStatus = c_int32(0)
        pUpgradeStatus = c_int32(0)
        return sc_cam_lib.scGetUpgradeStatus(self.device_handle, byref(pStatus), byref(pUpgradeStatus)), pStatus.value, pUpgradeStatus.value
    
    ### Morph related functions ###
    def scAIModuleSetEnabled(self, enable =  c_bool(True)):
        return sc_cam_lib.scAIModuleSetEnabled(self.device_handle, enable)

    def scAIModuleGetEnabled(self):
        enable = c_bool(True)
        return sc_cam_lib.scAIModuleGetEnabled(self.device_handle, byref(enable)), enable.value

    def scAIModuleSetParam(self, paramID = c_uint32(0), pBuffer = c_void_p(0), bufferSize = c_uint16(0)):
        return sc_cam_lib.scAIModuleSetParam(self.device_handle, paramID, pBuffer, bufferSize)

    def scAIModuleGetParam(self, paramID = c_uint32(0), pBuffer = c_void_p(0), bufferSize = c_uint16(0)):
        return sc_cam_lib.scAIModuleGetParam(self.device_handle, paramID, byref(pBuffer), byref(bufferSize))

    def scAIModuleSetWorkMode(self, mode = ScAIModuleMode(0)):
        return sc_cam_lib.scAIModuleSetWorkMode(self.device_handle, mode.value)

    def scAIModuleGetWorkMode(self):
        mode = c_uint8(0)
        return sc_cam_lib.scAIModuleGetWorkMode(self.device_handle, byref(mode)), mode.value

    def scAIModuleTriggerOnce(self):
        return sc_cam_lib.scAIModuleTriggerOnce(self.device_handle)

    def scAIModuleSetInputFrameTypeEnabled(self, frametype = ScFrameType.SC_DEPTH_FRAME, enable = c_bool(False)):
        return sc_cam_lib.scAIModuleSetInputFrameTypeEnabled(self.device_handle, frametype.value , enable)

    def scAIModuleGetInputFrameTypeEnabled(self ,frametype = ScFrameType.SC_DEPTH_FRAME):
        enable = c_bool(True)
        return sc_cam_lib.scAIModuleGetInputFrameTypeEnabled(self.device_handle, frametype.value, byref(enable)), enable.value

    def scAIModuleSetPreviewFrameTypeEnabled(self, frametype = ScFrameType.SC_DEPTH_FRAME, enable = c_bool(False)):
        return sc_cam_lib.scAIModuleSetPreviewFrameTypeEnabled(self.device_handle, frametype.value , enable)

    def scAIModuleGetPreviewFrameTypeEnabled(self ,frametype = ScFrameType.SC_DEPTH_FRAME):
        enable = c_bool(True)
        return sc_cam_lib.scAIModuleGetPreviewFrameTypeEnabled(self.device_handle, frametype.value, byref(enable)), enable.value

    def scAIModuleGetResult(self, waitTime = c_uint32(1000)):
        pAIResult = ScAIResult()
        return sc_cam_lib.scAIModuleGetResult(self.device_handle, waitTime, byref(pAIResult)), pAIResult

