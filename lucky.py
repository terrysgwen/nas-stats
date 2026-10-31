"""
Lucky (大吉) 穿透与反向代理联动模块
支持连接 Lucky 服务，自动拉取 Web 服务与反向代理规则列表，
并与本地服务卡片（内网 IP 与端口）进行智能对比匹配，赋能外网访问直达。
"""

import base64
import copy
import json
import logging
import re
import secrets
import time
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
        raw_url = (url or "").strip().rstrip("/")
        # 严格剥离末尾误配或自动补全的 /api，避免产生 /terry/api/api/...
        self.url = re.sub(r"/api/?$", "", raw_url, flags=re.IGNORECASE).rstrip("/")
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

        # 1. 尝试 Lucky 3.x RSA 挑战握手登录
        try:
            import secrets
            import base64
            from cryptography.hazmat.primitives.asymmetric import padding
            from cryptography.hazmat.primitives import serialization

            session_id = secrets.token_hex(32)
            ch_resp = await client.get(
                f"{self.url}/api/login/challenge",
                headers={"Lucky-Login-Session": session_id},
                timeout=self.timeout
            )
            if ch_resp.status_code == 200:
                ch_data = ch_resp.json()
                if ch_data.get("ret") == 0 and ch_data.get("publicKey"):
                    pubkey = serialization.load_pem_public_key(ch_data["publicKey"].encode('utf-8'))
                    payload_dict = {
                        "account": self.account,
                        "password": self.password,
                        "twoFA": "",
                        "challengeId": ch_data["challengeId"],
                        "nonce": ch_data["nonce"]
                    }
                    payload_str = json.dumps(payload_dict, separators=(',', ':'))
                    chunk_size = 120
                    cipher_parts = []
                    for i in range(0, len(payload_str), chunk_size):
                        part = payload_str[i:i+chunk_size].encode('utf-8')
                        enc = pubkey.encrypt(part, padding.PKCS1v15())
                        cipher_parts.append(base64.b64encode(enc).decode('ascii'))
                    cipher_text = ".".join(cipher_parts)

                    login_resp = await client.post(
                        f"{self.url}/api/login",
                        json={"challengeId": ch_data["challengeId"], "cipherText": cipher_text},
                        headers={"Lucky-Login-Session": session_id},
                        timeout=self.timeout
                    )
                    if login_resp.status_code == 200:
                        login_data = login_resp.json()
                        if login_data.get("ret") == 0:
                            return login_data.get("token") or ""
                        elif login_data.get("need2FA"):
                            raise Exception("Lucky 开启了双重验证 (2FA)，暂不支持密码自动登录，请在设置中改用「OpenToken 授权」")
                        else:
                            raise Exception(login_data.get("msg") or "Lucky 账号或密码错误")
        except Exception as e:
            if "双重验证" in str(e) or "账号或密码错误" in str(e):
                raise

        # 2. 回退到 Lucky 2.x 传统明文账号密码登录
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

    async def fetch_raw_rules(self) -> Dict[str, Any]:
        """从 Lucky 拉取原始 Web 服务 / 反向代理规则数据"""
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
                headers["Lucky-Admin-Token"] = token
                headers["token"] = token
            if self.open_token:
                headers["openToken"] = self.open_token

            # 候选规则查询路径 (跨版本自适应探测，Lucky 3.x 核心路径为 /api/webservice/rules)
            candidate_paths = [
                "/api/webservice/rules",
                "/api/webservice/rules_lite",
                "/api/reverseproxyrule",
                "/api/reverseproxyrule/list",
                "/api/webrule",
                "/api/webrule/list",
                "/api/reverseproxy/list",
                "/api/reverseproxy",
                "/openapi/webservice/rules",
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
                                if isinstance(json_res, dict) and json_res.get("ret") not in (0, None) and "login" in str(json_res.get("msg", "")).lower():
                                    last_err = "Lucky 鉴权失败，Token 或账号密码无效"
                                    continue
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

            return raw_data

    async def fetch_rules(self) -> List[Dict[str, Any]]:
        """从 Lucky 拉取 Web 服务 / 反向代理规则列表"""
        raw_data = await self.fetch_raw_rules()
        return self._parse_rules(raw_data)

    async def get_main_rules_summary(self) -> List[Dict[str, Any]]:
        """获取 Lucky 中已存在的所有主规则概要列表"""
        raw_data = await self.fetch_raw_rules()
        rule_list = []
        if isinstance(raw_data, list):
            rule_list = raw_data
        elif isinstance(raw_data, dict):
            if "ruleList" in raw_data and isinstance(raw_data["ruleList"], list):
                rule_list = raw_data["ruleList"]
            elif "data" in raw_data and isinstance(raw_data["data"], list):
                rule_list = raw_data["data"]

        summary = []
        for idx, r in enumerate(rule_list):
            if not isinstance(r, dict):
                continue
            r_key = str(r.get("RuleKey") or r.get("Key") or f"rule_{idx}")
            r_name = str(r.get("RuleName") or r.get("Remarks") or f"主规则-{r_key}")
            port = r.get("ListenPort") or 0
            try:
                port = int(port)
            except Exception:
                port = 0
            is_tls = bool(r.get("EnableTLS") or r.get("TLS") or port == 443)
            proxy_list = r.get("ProxyList") or r.get("SubRules") or []
            summary.append({
                "key": r_key,
                "name": r_name,
                "port": port,
                "tls": is_tls,
                "protocol": "https" if is_tls else "http",
                "subrules_count": len(proxy_list) if isinstance(proxy_list, list) else 0,
            })
        return summary

    async def add_subrule_to_main_rule(
        self,
        service_name: str,
        domain_or_url: str,
        target_url: str,
        main_rule_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """为指定的卡片地址在 Lucky 主规则下克隆同级配置并添加子规则"""
        if not self.url:
            raise Exception("未配置 Lucky 服务器地址")

        raw_data = await self.fetch_raw_rules()
        rule_list = []
        if isinstance(raw_data, list):
            rule_list = raw_data
        elif isinstance(raw_data, dict):
            if "ruleList" in raw_data and isinstance(raw_data["ruleList"], list):
                rule_list = raw_data["ruleList"]
            elif "data" in raw_data and isinstance(raw_data["data"], list):
                rule_list = raw_data["data"]

        if not rule_list:
            raise Exception("Lucky 中未找到任何 Web 服务的「主规则」，请先在 Lucky 后台【Web服务】创建一条主规则（设置监听端口与证书）后，即可自动添加子规则。")

        target_rule = None
        if main_rule_key:
            for r in rule_list:
                if str(r.get("RuleKey") or r.get("Key")) == str(main_rule_key):
                    target_rule = r
                    break

        if not target_rule:
            # 优先选择已有子规则的主规则作为载体
            for r in rule_list:
                pl = r.get("ProxyList") or r.get("SubRules")
                if isinstance(pl, list) and len(pl) > 0:
                    target_rule = r
                    break
            if not target_rule:
                target_rule = rule_list[0]

        # 解析用户填写的外部域名与可能包含的路径
        raw_input = domain_or_url.strip()
        clean = re.sub(r"^https?://", "", raw_input)
        path = ""
        if "/" in clean:
            domain_part, path_part = clean.split("/", 1)
            clean = domain_part
            path = "/" + path_part.rstrip("/")

        clean_domain = clean.split(":")[0].strip()
        if not clean_domain:
            raise Exception("请输入有效的外部访问域名或完整 URL (如 qb.example.com)")

        # 确保 ProxyList 存在
        proxy_list = target_rule.get("ProxyList")
        if not isinstance(proxy_list, list):
            proxy_list = target_rule.get("SubRules")
            if not isinstance(proxy_list, list):
                proxy_list = []
                target_rule["ProxyList"] = proxy_list

        new_sub_key = str(int(time.time() * 1000))
        if proxy_list:
            # 深度克隆该主规则下的已有子规则，实现“和其他同主规则下的子规则设置完全一致”
            template_sub = proxy_list[0]
            new_sub = copy.deepcopy(template_sub)
            new_sub["Key"] = new_sub_key
            new_sub["SubRuleKey"] = new_sub_key
            new_sub["Remarks"] = service_name
            new_sub["SubRuleName"] = service_name
            new_sub["Domains"] = [clean_domain]
            new_sub["Domain"] = clean_domain
            new_sub["Locations"] = [target_url]
            new_sub["TargetServer"] = target_url
            new_sub["Enable"] = True
            new_sub["WebServiceType"] = template_sub.get("WebServiceType") or "reverseproxy"
            if path:
                new_sub["MatchPath"] = path
                new_sub["Path"] = path
        else:
            new_sub = {
                "Key": new_sub_key,
                "SubRuleKey": new_sub_key,
                "Remarks": service_name,
                "SubRuleName": service_name,
                "Domains": [clean_domain],
                "Domain": clean_domain,
                "Locations": [target_url],
                "TargetServer": target_url,
                "Enable": True,
                "WebServiceType": "reverseproxy",
            }
            if path:
                new_sub["MatchPath"] = path
                new_sub["Path"] = path

        proxy_list.append(new_sub)

        # 计算生成的外部访问完整 URL
        listen_port = target_rule.get("ListenPort") or 0
        try:
            listen_port = int(listen_port)
        except Exception:
            listen_port = 0

        is_tls = bool(target_rule.get("EnableTLS") or target_rule.get("TLS") or listen_port == 443)
        proto = "https" if is_tls else "http"
        if (is_tls and listen_port in (443, 0)) or (not is_tls and listen_port in (80, 0)):
            ext_url = f"{proto}://{clean_domain}{path}"
        else:
            ext_url = f"{proto}://{clean_domain}:{listen_port}{path}"

        # 调用 Lucky API 保存主规则 (更新其 ProxyList)
        async with httpx.AsyncClient(verify=False, timeout=self.timeout, follow_redirects=True) as client:
            token = await self.get_token(client)
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            if token:
                headers["Authorization"] = token
                headers["openToken"] = token
                headers["Lucky-Admin-Token"] = token
                headers["token"] = token
            if self.open_token:
                headers["openToken"] = self.open_token

            save_url = f"{self.url}/api/webservice/rules"
            try:
                resp = await client.post(save_url, json=target_rule, headers=headers)
            except Exception as e:
                raise Exception(f"连接 Lucky 保存子规则失败: {e}")

            if resp.status_code == 200:
                try:
                    res_data = resp.json()
                    if isinstance(res_data, dict) and res_data.get("ret") not in (0, None):
                        raise Exception(res_data.get("msg") or "Lucky 保存子规则失败")
                except Exception as e:
                    if "Lucky 保存子规则失败" in str(e):
                        raise
            elif resp.status_code == 404:
                # 兼容部分老版本单个子规则或规则保存接口
                try:
                    resp2 = await client.post(f"{self.url}/api/reverseproxyrule", json=target_rule, headers=headers)
                    if resp2.status_code != 200:
                        raise Exception(f"Lucky 保存失败 (HTTP {resp2.status_code})")
                except Exception as e2:
                    raise Exception(f"Lucky 保存接口返回 404，请确认 Lucky 版本与权限: {e2}")
            else:
                raise Exception(f"Lucky 保存接口返回异常状态码: HTTP {resp.status_code}")

        return {
            "ok": True,
            "main_rule_key": str(target_rule.get("RuleKey") or target_rule.get("Key")),
            "main_rule_name": target_rule.get("RuleName") or f"端口-{listen_port}",
            "sub_rule_key": new_sub_key,
            "external_url": ext_url,
        }

    def _parse_rules(self, raw_data: Any) -> List[Dict[str, Any]]:
        """将不同结构版本的 Lucky 规则标准化解析"""
        items = []
        if isinstance(raw_data, list):
            items = raw_data
        elif isinstance(raw_data, dict):
            # Lucky 3.x 使用 ruleList
            if "ruleList" in raw_data and isinstance(raw_data["ruleList"], list):
                items = raw_data["ruleList"]
            elif "data" in raw_data:
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
            tls_flag = item.get("TLS") or item.get("Tls") or item.get("tls") or item.get("EnableTLS") or item.get("EnableTls")
            if "https" in raw_proto or tls_flag or listen_port == 443:
                protocol = "https"
            else:
                protocol = "http"

            domains_raw = item.get("DomainList") or item.get("Domains") or item.get("Domain") or item.get("domains") or ""
            # Lucky 3.x 子规则字段为 ProxyList
            sub_rules = item.get("ProxyList") or item.get("SubRules") or item.get("subRules") or item.get("SubRuleList") or []

            if not isinstance(sub_rules, list) or len(sub_rules) == 0:
                sub_rules = [item]

            for sidx, sub in enumerate(sub_rules):
                if not isinstance(sub, dict):
                    continue

                sub_rule_key = str(sub.get("SubRuleKey") or sub.get("subRule") or sub.get("Key") or f"sub_{sidx}")
                sub_name = str(sub.get("SubRuleName") or sub.get("Remarks") or sub.get("remarks") or sub.get("Name") or rule_name or "")
                
                # 目标后端地址 (Lucky 3.x 存放在 Locations 数组中)
                target_str = ""
                locs = sub.get("Locations") or sub.get("locations")
                if isinstance(locs, list) and locs:
                    target_str = str(locs[0]).strip()
                elif isinstance(locs, str) and locs.strip():
                    target_str = locs.strip()
                else:
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
                sub_domains = sub.get("Domains") or sub.get("Domain") or sub.get("DomainList") or domains_raw
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
                    "service_type": str(sub.get("WebServiceType") or sub.get("serviceType") or sub.get("ServiceType") or "reverseproxy"),
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
