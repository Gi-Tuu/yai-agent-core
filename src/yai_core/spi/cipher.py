"""静态存储加密契约：保护落盘的敏感记忆（第九个 SPI）。

model / channel / memory / policy / discovery / sandbox / learning / embedding
之后，cipher 是第九个 SPI。它解决一个具体问题：:class:`~yai_core.memory.MemoryVault`
默认把长期记忆以明文 JSON 落盘，若宿主经 ``remember`` 沉淀了个人画像、健康、
关系等敏感信息，磁盘 / 备份 / 同步盘上的文件就会直接暴露这些内容。

内核**只定义契约、不内置任何加密算法、不引入加密库**（零硬依赖红线）：

- 真实实现（如 Fernet / AES-GCM，密钥来自宿主的环境变量、KMS 或系统钥匙串）
  放在可选 extra，懒加载；
- 离线测试用确定性的可逆 Fake  cipher（如标准库 base64，仅验证接线，不提供安全性）；
- 不配置 cipher 时维持现状：明文 JSON 落盘，并在文档中明确声明，宿主自行权衡。

接口设计为**同步**：vault 的持久化本来就是同步文件 IO（原子写），加密/解密也应
同步完成，避免为落盘引入 async 传染。字节进、字节出，与具体编码解耦。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class StorageCipher(Protocol):
    """对落盘字节做对称加密 / 解密的契约。

    实现要求：

    - ``encrypt(plaintext) -> ciphertext`` 与 ``decrypt(ciphertext) -> plaintext``
      互为逆运算，对同一密钥满足 ``decrypt(encrypt(b)) == b``；
    - 密钥由实现方持有，内核不接触密钥来源；
    - 解密遇到篡改 / 密钥不符 / 数据损坏时抛异常，调用方按"文件损坏、冷启动容忍"
      处理（vault 会退化为空库并在下次保存时覆盖，不会崩溃）。
    """

    def encrypt(self, plaintext: bytes) -> bytes:
        """加密任意明文字节，返回可直接写盘的密文字节。"""
        ...

    def decrypt(self, ciphertext: bytes) -> bytes:
        """解密 :meth:`encrypt` 产生的密文字节；失败（篡改/密钥不符）时抛异常。"""
        ...
