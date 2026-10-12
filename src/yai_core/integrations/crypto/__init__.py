"""静态存储加密集成（可选 ``crypto`` extra，懒加载 cryptography）。

    from yai_core.integrations.crypto import FernetStorageCipher

内核本体不依赖 cryptography；未安装 ``crypto`` extra 时模块仍可导入，只有真正
构造 cipher 才会抛出带安装指引的 ImportError。
"""

from yai_core.integrations.crypto.fernet_cipher import FernetStorageCipher

__all__ = ["FernetStorageCipher"]
