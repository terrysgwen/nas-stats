"""
Lucky (大吉) 穿透与反向代理联动模块
支持连接 Lucky 服务，自动拉取 Web 服务与反向代理规则列表，
并与本地服务卡片（内网 IP 与端口）进行智能对比匹配，赋能外网访问直达。
"""

import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse
import httpx

logger = logging.getLogger("lucky")


def parse_target_host_port(target_str: str) -> Tuple[str, int]:
    """
    解析 Lucky 目标后端地址中的 Host 与 Port。
    例如:
      "http://192.168.1.50:5244" -> ("192.168.1.50", 5244)
      "https://192.168.1.50:8006/" -> ("192.168.1.50", 8006)
      "192.168.1.50:5244" -> ("192.168.1.50", 5244)
      "http://127.0.0.1:5666" -> ("127.0.0.1", 5666)
      "localhost:80" -> ("localhost", 80)
      "http://nas.lan" -> ("nas.lan", 80)
    """
    if not target_str or not isinstance(target_str, str):
        return "", 0

    target = target_str.strip()
    if not target.startswith("http://") and not target.startswith("https://"):
        target = "http://" + target

    try:
        parsed = urlparse(target)
        host = (parsed.hostname or "").lower()
        port = parsed.port
        if not port:
            port = 443 if parsed.scheme == "https" else 80
        return host, int(port)
    except Exception:
        # 正则兜底解析
        m = re.search(r"://([^:/]+)(?::(\d+))?", target)
        if m:
            host = m.group(1).lower()
            port = int(m.group(2)) if m.group(2) else 80
            return host, port
        return "", 0


class LuckyClient:
    """Lucky (大吉) API 客户端"""

    def __init__(
        self,
        url: str,
        auth_type: str = "login",
        account: str = "666",
        password: str = "666",
        open_token: str = "",
        timeout: float = 6.0,
    ):
        self.url = (url or "").strip().rstrip("/")
        self.auth_type = auth_type or "login"
        self.account = account or "666"
        self.password = password or "666"
        self.open_token = (open_token or "").strip()
        self.timeout = timeout

    async def get_token(self, client: httpx.AsyncClient) -> str:
        """获取用于 API 调用的 Token"""
        if self.auth_type == "token":
            if not self.open_token:
                raise Exception("未配置 OpenToken，请在 Lucky 设置最底部开启并填入 OpenToken")
            return self.open_token

        # 账号密码登录模式
        login_url = f"{self.url}/api/login"
        payload = {"Account": self.account, "Password": self.password}
        try:
            resp = await client.post(login_url, json=payload, timeout=self.timeout)
        except Exception as e:
            raise Exception(f"无法连接到 Lucky 服务器: {e}")

        if resp.status_code == 200:
            try:
                data = resp.json()
            except Exception:
                raise Exception("Lucky 登录响应非 JSON 格式")

            ret = data.get("ret")
            token = data.get("token") or ""
            if ret == 0 or token or data.get("code") == 200:
                if not token and isinstance(data.get("data"), dict):
                    token = data["data"].get("token", "")
                if token:
                    return token
                # 部分版本通过 Cookie 鉴权
                return ""
            raise Exception(data.get("msg") or data.get("message") or "Lucky 账号或密码错误")
        elif resp.status_code == 404:
            # 兼容老版本 /login 接口
            try:
                resp2 = await client.post(f"{self.url}/login", json=payload, timeout=self.timeout)
                if resp2.status_code == 200:
                    data2 = resp2.json()
                    return data2.get("token") or ""
            except Exception:
                pass
            raise Exception(f"Lucky 登录失败 (HTTP {resp.status_code})")
        else:
            raise Exception(f"Lucky 登录接口返回异常状态码: {resp.status_code}")

    async def fetch_rules(self) -> List[Dict[str, Any]]:
        """从 Lucky 拉取 Web 服务 / 反向代理规则列表"""
        if not self.url:
            raise Exception("未配置 Lucky 服务器地址")

        async with httpx.AsyncClient(verify=False, timeout=self.timeout, follow_redirects=True) as client:
            token = await self.get_token(client)

            headers = {
                "Accept": "application/json",
            }
            if token:
                headers["Authorization"] = token
                headers["openToken"] = token
            if self.open_token:
                headers["openToken"] = self.open_token

            # 候选规则查询路径 (跨版本自适应探测)
            candidate_paths = [
                "/api/reverseproxyrule",
                "/api/reverseproxyrule/list",
                "/api/webrule",
                "/api/webrule/list",
                "/api/reverseproxy/list",
                "/api/reverseproxy",
                "/openapi/webseivce/list",
                "/openapi/webservice/list",
            ]

            raw_data = None
            last_err = None

            for path in candidate_paths:
                target_url = f"{self.url}{path}"
                try:
                    r = await client.get(target_url, headers=headers)
                    if r.status_code == 200:
                        try:
                            json_res = r.json()
                            if isinstance(json_res, (list, dict)):
                                raw_data = json_res
                                logger.info(f"成功从 Lucky {path} 获取规则")
                                break
                        except Exception:
                            continue
                    elif r.status_code == 401:
                        last_err = "Lucky 鉴权失败，Token 或账号密码无效"
                except Exception as e:
                    last_err = str(e)
                    continue

            if raw_data is None:
                if last_err:
                    raise Exception(f"拉取 Lucky 规则失败: {last_err}")
                raise Exception("无法从 Lucky 探测到有效的 Web 服务反代规则列表接口，请检查 Lucky 运行状态与权限设置")

            return self._parse_rules(raw_data)

    def _parse_rules(self, raw_data: Any) -> List[Dict[str, Any]]:
        """将不同结构版本的 Lucky 规则标准化解析"""
        items = []
        if isinstance(raw_data, list):
            items = raw_data
        elif isinstance(raw_data, dict):
            # 常见包装: {"data": [...]}, {"list": [...]}, {"data": {"list": [...]}}
            if "data" in raw_data:
                d = raw_data["data"]
                if isinstance(d, list):
                    items = d
                elif isinstance(d, dict) and "list" in d and isinstance(d["list"], list):
                    items = d["list"]
                elif isinstance(d, dict):
                    items = list(d.values())
            elif "list" in raw_data and isinstance(raw_data["list"], list):
                items = raw_data["list"]
            else:
                items = list(raw_data.values())

        normalized = []
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                continue

            rule_key = str(item.get("RuleKey") or item.get("rule") or item.get("Key") or f"rule_{idx}")
            rule_name = str(item.get("RuleName") or item.get("Remarks") or item.get("remarks") or item.get("name") or "")
            listen_port = item.get("ListenPort") or item.get("Port") or item.get("listenPort") or 0
            try:
                listen_port = int(listen_port)
            except Exception:
                listen_port = 0

            # 协议识别
            raw_proto = str(item.get("ListenProtocol") or item.get("Protocol") or item.get("protocol") or "").lower()
            tls_flag = item.get("TLS") or item.get("Tls") or item.get("tls") or item.get("EnableTls")
            if "https" in raw_proto or tls_flag or listen_port == 443:
                protocol = "https"
            else:
                protocol = "http"

            domains_raw = item.get("DomainList") or item.get("Domains") or item.get("Domain") or item.get("domains") or ""
            sub_rules = item.get("SubRules") or item.get("subRules") or item.get("SubRuleList") or []

            if not isinstance(sub_rules, list) or len(sub_rules) == 0:
                sub_rules = [item]

            for sidx, sub in enumerate(sub_rules):
                if not isinstance(sub, dict):
                    continue

                sub_rule_key = str(sub.get("SubRuleKey") or sub.get("subRule") or sub.get("Key") or f"sub_{sidx}")
                sub_name = str(sub.get("Remarks") or sub.get("remarks") or sub.get("Name") or rule_name or "")
                target_str = str(
                    sub.get("TargetServer")
                    or sub.get("target")
                    or sub.get("Target")
                    or sub.get("location")
                    or sub.get("Location")
                    or sub.get("ProxyTarget")
                    or ""
                ).strip()

                if not target_str:
                    continue

                target_host, target_port = parse_target_host_port(target_str)
                if not target_port:
                    continue

                # 域名解析 (子规则优先，主规则兜底)
                sub_domains = sub.get("Domain") or sub.get("DomainList") or domains_raw
                domain = ""
                if isinstance(sub_domains, list) and sub_domains:
                    domain = str(sub_domains[0]).strip()
                elif isinstance(sub_domains, str) and sub_domains.strip():
                    # 多个域名可能以逗号、空格或换行分隔
                    first_dom = re.split(r"[,;\s\n]+", sub_domains.strip())[0]
                    domain = first_dom.strip()

                # 路径匹配
                match_path = str(sub.get("MatchPath") or sub.get("Path") or sub.get("path") or "").strip()
                if match_path and not match_path.startswith("/"):
                    match_path = "/" + match_path

                # 外部可访问 URL 构建
                ext_url = ""
                if domain:
                    clean_dom = domain.replace("http://", "").replace("https://", "").rstrip("/")
                    if protocol == "https" and listen_port in (443, 0):
                        ext_url = f"https://{clean_dom}{match_path}"
                    elif protocol == "http" and listen_port in (80, 0):
                        ext_url = f"http://{clean_dom}{match_path}"
                    else:
                        ext_url = f"{protocol}://{clean_dom}:{listen_port}{match_path}"

                normalized.append({
                    "rule_key": rule_key,
                    "sub_rule_key": sub_rule_key,
                    "name": sub_name or domain or f"规则-{listen_port}",
                    "domain": domain,
                    "listen_port": listen_port,
                    "protocol": protocol,
                    "target": target_str,
                    "target_host": target_host,
                    "target_port": target_port,
                    "external_url": ext_url,
                    "service_type": str(sub.get("serviceType") or sub.get("ServiceType") or "reverseproxy"),
                })

        return normalized


def match_services_with_lucky(
    services: List[Dict[str, Any]],
    lucky_rules: List[Dict[str, Any]],
    nas_ip: str = "",
) -> List[Dict[str, Any]]:
    """
    智能对比本地服务卡片与 Lucky Web 服务规则，生成匹配对照清单。
    对比策略:
      1. 精确匹配: target_port 相等，且 (target_host 相等 或 为本地回环 127.0.0.1/localhost/0.0.0.0 或等于 nas_ip)
      2. 端口唯一匹配: target_port 相等
    """
    clean_nas_ip = (nas_ip or "").strip().lower()
    matched_results = []

    for s in services:
        sid = str(s.get("id", ""))
        sname = str(s.get("name", ""))
        sport = s.get("port")
        surl = str(s.get("url", "")).strip()
        swan = str(s.get("wan_url", "")).strip()

        # 解析卡片实际端口与主机
        card_port = 0
        card_host = ""
        if sport and int(sport) > 0:
            card_port = int(sport)
        if surl:
            h, p = parse_target_host_port(surl)
            if not card_port and p:
                card_port = p
            card_host = h

        if not card_host:
            card_host = clean_nas_ip

        # 执行匹配
        matched_rule = None

        # 策略 1: 目标端口与目标主机精准一致 (考虑 127.0.0.1 等价性)
        for r in lucky_rules:
            r_port = r.get("target_port")
            r_host = r.get("target_host", "").lower()

            if card_port and r_port == card_port:
                is_local_equivalent = (
                    r_host in ("127.0.0.1", "localhost", "0.0.0.0")
                    or card_host in ("127.0.0.1", "localhost", "0.0.0.0")
                    or (clean_nas_ip and r_host == clean_nas_ip)
                    or (clean_nas_ip and card_host == clean_nas_ip)
                    or (r_host == card_host)
                )
                if is_local_equivalent:
                    matched_rule = r
                    break

        # 策略 2: 端口唯一匹配
        if not matched_rule and card_port:
            candidates = [r for r in lucky_rules if r.get("target_port") == card_port]
            if len(candidates) == 1:
                matched_rule = candidates[0]
            elif len(candidates) > 1:
                # 优先匹配有外部域名的
                with_dom = [r for r in candidates if r.get("external_url")]
                if with_dom:
                    matched_rule = with_dom[0]
                else:
                    matched_rule = candidates[0]

        is_matched = bool(matched_rule and matched_rule.get("external_url"))
        lucky_ext_url = matched_rule.get("external_url", "") if matched_rule else ""
        lucky_target = matched_rule.get("target", "") if matched_rule else ""
        lucky_rule_name = matched_rule.get("name", "") if matched_rule else ""

        matched_results.append({
            "id": sid,
            "name": sname,
            "icon": s.get("icon") or "🔧",
            "favicon": s.get("favicon") or "",
            "category": s.get("category") or "自定义",
            "port": card_port,
            "internal_url": surl or (f"http://{clean_nas_ip or '127.0.0.1'}:{card_port}" if card_port else ""),
            "wan_url": swan,
            "matched": is_matched,
            "lucky_url": lucky_ext_url,
            "lucky_target": lucky_target,
            "lucky_rule_name": lucky_rule_name,
        })

    return matched_results
