"""原始算法 (timesai_tools C 扩展) 的 Python 封装.

timesai_tools.cp38-win_amd64.pyd 编译于 Python 3.8.10, 与同目录下的
opencv_*.dll / msvcp140.dll / vcruntime140.dll 一起加载.

注意: 坐标已是全图坐标 (非 ROI 内部坐标), 可直接用于绘制.
"""
import os
import sys

# 本模块所在目录 (即 original_algo/), .pyd 和 .dll 都在这里
_DIR = os.path.dirname(os.path.abspath(__file__))

# Python 3.8+ 不再从 sys.path 搜索 DLL, 需显式添加 DLL 目录
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)
if hasattr(os, 'add_dll_directory'):
    os.add_dll_directory(_DIR)

import cv2
import numpy as np
import timesai_tools

# 输出类型 / 找圆类型 / 是否输出构成点 (供 UI 引用)
output_types = ("0原图", "1轮廓图", "3线段图",
                "4弧段图", "5弧段合并", "6筛选圆", "7验证圆", "8结果构成点")
circle_type = ("最大圆", "最小圆", "所有圆", "最大圆和最小圆")
output_points = ("是", "否")


def _safe_circle_num(ret):
    """安全获取 circle_num, 避免空列表导致 IndexError."""
    try:
        if ret and ret[2] and ret[2][0] and len(ret[2][0]) > 0:
            return ret[2][0][0]
    except (IndexError, TypeError):
        pass
    return 0


def detect_circle_imp(images, rects, output_type=output_types[1],
                      circle_type=circle_type[0], output_point=output_points[0],
                      min_radius=0, max_radius=999999.9,
                      min_cover_ratio=0.3, max_cover_ratio=1.0,
                      grad_thresh=50.0, anchor_thresh=8.0,
                      min_path_len=10.0, guass_sigma=1.0, arc_fit_error=1.0,
                      circle_fit_error=2.0, min_arc_points=0, max_arc_points=999999,
                      circle_validation_pro=0.1, resize_factor=1.0, thread_num=2):
    """根据提取弧段、弧段合并成圆的方法, 根据筛选条件找出想要的圆或圆弧.

    参数:
        images: 图像列表
        rects: 运算区域, 格式 [[rect], ...], rect=[type, x, y, w, h, angle]
        output_type: 图像输出类型
        circle_type: 找圆类型 (最大圆/最小圆/所有圆/最大圆和最小圆)
        output_point: 是否输出构成圆的点
        min_radius / max_radius: 半径范围
        min_cover_ratio / max_cover_ratio: 圆弧完整度范围
        grad_thresh: 梯度阈值
        anchor_thresh: 连接点梯度
        min_path_len: 最小边缘长度
        guass_sigma: 高斯滤波 sigma
        arc_fit_error: 弧段允许误差
        circle_fit_error: 圆拟合误差
        min_arc_points / max_arc_points: 弧段点数范围
        circle_validation_pro: 圆验证概率
        resize_factor: 缩放系数 (算法加速)
        thread_num: 线程数

    返回:
        dict, 包含 statues/error_msg/circle_num/circles/circle_centers/
        radius/diameters/circle_points/circle_coverage/output_images/circles_for_show.
        注意: circles_for_show 元素格式为 [flag, cx, cy, r], 坐标为全图坐标.
    """
    inputs = {'0': images, '1': rects, '2': rects, '3': output_type,
              '4': circle_type, '5': output_point,
              '6': min_radius, '7': max_radius,
              '8': min_cover_ratio, '9': max_cover_ratio,
              '10': grad_thresh, '11': anchor_thresh,
              '12': min_path_len, '13': guass_sigma, '14': arc_fit_error,
              '15': circle_fit_error, '16': min_arc_points, '17': max_arc_points,
              '18': circle_validation_pro, '19': resize_factor, '20': thread_num}

    # 多级 resize 重试: 1.0 → 0.5 → 0.2, 提升困难场景的检出率
    ret = timesai_tools.detect_circle(inputs)
    cn = _safe_circle_num(ret)
    if cn == 0:
        inputs['19'] = 0.5
        ret = timesai_tools.detect_circle(inputs)
        cn = _safe_circle_num(ret)
        if cn == 0:
            inputs['19'] = 0.2
            ret = timesai_tools.detect_circle(inputs)

    convert_ret = {
        "statues": ret[0], "error_msg": ret[1], "circle_num": ret[2],
        "circles": ret[3], "circle_centers": ret[4], "radius": ret[5],
        "diameters": ret[6], "circle_points": ret[7],
        "circle_coverage": ret[8], "output_images": ret[9],
        "circles_for_show": ret[10],
    }
    return convert_ret


if __name__ == '__main__':
    # 独立测试: 加载测试图片并检测
    img_path = os.path.join(_DIR, '..', 'test_image', 'camera_2_20260429_182027.jpg')
    img = cv2.imread(img_path, 1)
    if img is None:
        print(f"无法读取图片: {img_path}")
        sys.exit(1)
    rect = [2, 328, 309, 150, 137, 0]
    ret = detect_circle_imp([img], [[rect]])
    print(f"circle_centers={ret['circle_centers']}, radius={ret['radius']}")
    if ret['circle_centers'] and ret['circle_centers'][0]:
        cc = ret['circle_centers'][0][0]
        x, y = int(cc[1]), int(cc[2])
        r = int(ret['radius'][0][0])
        gg = img.copy()
        cv2.circle(gg, (x, y), r, (0, 0, 255), 2)
        cv2.namedWindow('result', cv2.WINDOW_NORMAL)
        cv2.imshow('result', gg)
        cv2.waitKey(0)
