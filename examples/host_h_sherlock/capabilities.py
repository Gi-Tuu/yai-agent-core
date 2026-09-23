"""host_h 的对外能力：把 sherlock 包成一个干净的查询函数。

关键设计：
- sherlock 原本是 CLI 工具，内部 ``sherlock()`` 要传 ``site_data`` / ``query_notify``
  这类 CLI 风格对象，不适合直接交给 LLM。
- 我们在外面包一层：加载站点清单、收集回调结果、过滤"已注册"站点，
  只暴露 ``username`` / ``limit`` / ``timeout`` 三个语义清晰的参数。
- 不改 sherlock 源码；sherlock 本身的 import 放在函数内，未安装 sherlock
  时宿主其余部分仍可用（可选依赖）。
"""

from __future__ import annotations


def lookup_username(
    username: str,
    limit: int | None = None,
    timeout: int = 10,
) -> dict:
    """查一个用户名在哪些社交平台上已经注册了账号。

    会联网访问各站点首页做探测；结果按"已注册（Claimed）"过滤。

    Args:
        username: 要查询的用户名，例如 ``torvalds``。
        limit: 最多检查多少个站点；None 表示全部（数百个，较慢），
            演示时建议 10~20。
        timeout: 每个站点的超时秒数。

    Returns:
        字典，含 ``username``、``checked``（检查了几个站）、
        ``found``（命中的站点列表，每项 ``{"site": ..., "url": ...}``）。
    """
    # 延迟 import：sherlock 是可选依赖，没装也不影响宿主其余部分。
    from sherlock_project.notify import QueryNotify
    from sherlock_project.sherlock import sherlock
    from sherlock_project.sites import SitesInformation

    # LLM 常把数字参数传成字符串，归一化避免 int/str 比较错误。
    limit = int(limit) if limit is not None else None
    timeout = int(timeout)

    class _Collector(QueryNotify):
        """把 sherlock 的逐站回调收进内存，不打印。"""

        def __init__(self) -> None:
            super().__init__()
            self.found: list[dict] = []

        def update(self, result, message=None):  # type: ignore[override]
            status = getattr(result, "status", None)
            verbose = getattr(status, "verbose_name", "") or ""
            if verbose == "Claimed":
                self.found.append(
                    {"site": result.site_name, "url": result.url_user}
                )

    sites = SitesInformation()
    site_data: dict = {}
    for i, site in enumerate(sites):
        if limit is not None and i >= limit:
            break
        site_data[site.name] = site.information

    collector = _Collector()
    sherlock(
        username=username,
        site_data=site_data,
        query_notify=collector,
        dump_response=False,
        proxy=None,
        timeout=timeout,
    )
    return {
        "username": username,
        "checked": len(site_data),
        "found": collector.found,
    }
