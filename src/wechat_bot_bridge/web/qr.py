"""管理端微信登录二维码渲染。"""

from __future__ import annotations

from io import BytesIO

import qrcode
from qrcode.image.svg import SvgPathImage


def render_login_qr_svg(content: str) -> bytes:
    """把敏感二维码内容编码为图片，不向浏览器暴露原始 URL 字符串。"""

    qr = qrcode.QRCode(border=3, box_size=8)
    qr.add_data(content)
    qr.make(fit=True)
    image = qr.make_image(image_factory=SvgPathImage)
    output = BytesIO()
    image.save(output)
    return output.getvalue()
