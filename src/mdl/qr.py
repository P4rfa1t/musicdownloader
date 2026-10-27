"""二维码图片生成。

单独一层的原因：qrcode + Pillow 是可选依赖（打包体积各 2-4 MB），
没装时登录流程要能降级成「自己把链接生成二维码」，而不是整个功能崩掉。
"""

from __future__ import annotations

import base64
import io
import logging

log = logging.getLogger("mdl.qr")


def to_data_url(text: str, box_size: int = 8, border: int = 2) -> str:
    """把文本编码成 PNG 二维码的 data URL；依赖缺失或编码失败时返回空串。"""
    if not text:
        return ""
    try:
        import qrcode
        from qrcode.constants import ERROR_CORRECT_M
    except ImportError:
        log.warning("未安装 qrcode，无法生成二维码图片")
        return ""
    try:
        qr = qrcode.QRCode(
            version=None,
            error_correction=ERROR_CORRECT_M,
            box_size=box_size,
            border=border,
        )
        qr.add_data(text)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        # qrcode 的 image 工厂返回的是 PilImage 包装类，转成真正的 PIL.Image 再存
        pil = getattr(img, "_img", img)
        pil.save(buf, format="PNG", optimize=True)
    except Exception as exc:  # noqa: BLE001 - 编码失败不该拖垮登录流程
        log.warning("二维码生成失败: %s", exc)
        return ""
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def available() -> bool:
    """依赖齐不齐。"""
    try:
        import PIL  # noqa: F401
        import qrcode  # noqa: F401
    except ImportError:
        return False
    return True