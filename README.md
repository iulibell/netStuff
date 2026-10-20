# Enterprise Topology — 企业全网联调（eNSP）

> A carrier-grade enterprise network simulation built on **Huawei eNSP**:
> MPLS/BGP L3VPN backbone · IS-IS IGP · firewall HA + NAT Server · GRE over IPSec · wireless & NOC.
> 基于华为 eNSP 搭建的运营商级企业全网联调实验：MPLS/BGP L3VPN 骨干、IS-IS、防火墙 HA + NAT Server 发布、GRE over IPSec、无线与网管。

---

## 1. 项目概况 / Overview

| 项目 | 内容 |
|---|---|
| 模拟平台 | Huawei eNSP（AR2220 / S5700 / USG6000V / AC / AP / Cloud） |
| 规模 | **97 台设备 · ~124 条链路** |
| 角色 | 独立完成拓扑设计、全网配置、端到端联调与故障定位 |
| 核心协议 | IS-IS、MPLS/LDP、MP-BGP VPNv4(L3VPN)、RR、VRRP、NAT Server、IPSec、OSPF、VLAN、AC/AP |

**一句话描述**：模拟「运营商骨干网 + 城域网 + 企业总部/分部 + 外部用户接入」的完整网络，让外部互联网用户经 MPLS L3VPN 骨干访问企业总部/分部对外发布的 HTTP 网站，并实现总部—分部站间加密互通。

---

## 2. 拓扑架构 / Architecture

```
                          ┌──────────────────────── 运营商 MPLS/BGP L3VPN 骨干 ────────────────────────┐
                          │        IS-IS 100 (L2, cost-style wide) + MPLS LDP + BGP 65000 VPNv4       │
                          │        RR = BACKBONE1 / BACKBONE2   VPN-instance = enterprise (RT 100:1) │
                          │                                                                            │
   ┌──────────┐           │   ┌─────────┐        ┌─────────┐        ┌─────────┐        ┌─────────┐  │
   │ HEAD 总部 │           │   │BACKBONE1│◄──────►│BACKBONE2│◄──────►│BACKBONE3│◄──────►│BACKBONE4│  │
   │ HEAD_FW1 │   .17      │   │  (RR)   │        │  (RR)   │        │   P     │        │   P     │  │
   │ HEAD_FW2 │◄─LSW18────►│ISP1(PE) │        │         │        │         │        │         │  │
   │ (VRRP HA)│  .18/.19   │   ▲      │        │         │        │   ▲     │        │   ▲     │  │
   │  VIP .20 │            │   │      │        │         │        │   │     │        │   │     │  │
   └────┬─────┘            │ METRO_A1│        │METRO_A2 │        │METRO_B1 │        │METRO_B2 │  │
        │ (SERVER_FW 双层墙) │   ▲      │        │   ▲     │        │   ▲     │        │   ▲     │  │
   ┌────▼─────────┐        │   │      │        │   │     │        │   │     │        │   │     │  │
   │ 服务器区       │        │ ISP2(PE) │        │ISP4(PE) │        │ ISP3(PE)│        │          │  │
   │ 10.1.1.0/24  │        │  NOC 网管 │        │ NOC     │        │   .1    │        │          │  │
   │ HEAD_HTTP/DNS│        └───────────┘        └─────────┘        └────┬────┘        └──────────┘  │
   └──────────────┘                                                    │                            │
                                                                       │ 200.147.10.0/29            │
                          ┌────────────────────────────────────────────┼────────────────────────────┤
                          │                                            ▼                            │
                          │                                       ┌─────────┐                      │
   ┌──────────────┐       │          ┌──────────┐                 │ DIVI_FW │  ┌─────────────────┐  │
   │ 用户接入网     │       │          │Client_SW │                 └────┬────┘  │ DIVI 分部        │  │
   │ 200.147.50.0/24│◄─────┤          │ (VLAN 50)│                      │       │ 192.168.151/152 │  │
   │ USER_CLIENT  │ .50.1 │          └────┬─────┘                      │       │ /153.0/24       │  │
   │ 1/2/3        │       │   METRO_A1   │ (用户接入 PE, RD 100:21)      │       │ DIVI_HTTP/DNS   │  │
   └──────────────┘       │   GE0/0/3    └──────────────────────────────┘       └─────────────────┘  │
                          └─────────────────────────────────────────────────────────────────────────────┘

   无线网：AC6005/AC6605 + AP6050 ×5 + STA ×6 + AAA_SERVER
   分支机构：BR_CORE1/2 + BR_SW1 + BR_PC1 + BR_SERVER
```

**设计要点**
- 骨干 13 台路由器（5 BB + 4 ISP + 4 METRO）统一运行 **IS-IS 100**（单区域 Level-2、cost-style wide），Loopback `192.168.255.x/32` 作为 MPLS LSR-ID 与 BGP 邻居源。
- **MPLS LDP** 沿 IS-IS 路径构建外层 LSP；**MP-BGP VPNv4**（AS 65000，BACKBONE1/2 做 RR，`undo policy vpn-target` + `reflect-client`）分发 VPN 路由与内层标签。
- VPN 实例 `enterprise` 三站点互通：HEAD（RD 100:1）/ DIVI（RD 100:3）/ 用户接入（RD 100:21），**RT 统一 100:1**。
- 企业侧 **双层防火墙**：HEAD_FW(边界, VRRP HA) → SERVER_FW(服务器区)，服务器区走 OSPF。
- 网站发布用 **NAT Server（DNAT）+ security-policy 放行**；站间用 **GRE over IPSec** 加密。

---

## 3. 技术栈 / Tech Stack

| 类别 | 技术 | 用途 |
|---|---|---|
| IGP | IS-IS 100（Level-2 / cost-style wide） | 骨干全网路由、LDP/BGP 的底层可达 |
| 二层 | VLAN、Access/Trunk | 接入/汇聚/核心交换机、用户接入 |
| MPLS | MPLS + LDP、PHP（implicit-null） | 构建端到端外层 LSP |
| VPN | MP-BGP VPNv4、RR、VPN-instance、RD/RT | 三站点 L3VPN 互通 |
| 高可用 | VRRP（vrid 147） | HEAD_FW1/FW2 防火墙主备 |
| 安全 | NAT Server(DNAT)、server-map、security-policy、GRE over IPSec | 网站发布、站间加密 |
| 服务器区 | OSPF 1（area 0，明文认证） | HEAD_FW ↔ SERVER_FW 路由 |
| 无线 | AC6605/AC6005 + AP6050 + STA + AAA | 无线接入 |
| 网管 | NMS_SERVER + OPS_PC | 设备集中管理 |

---

## 4. 关键地址与设计汇总 / Addressing Summary

> 本表为**唯一收口地址表**（已合并早期占位地址），以拓扑实际 settings 为准。

| 设备 | Loopback / SystemID / NET | 关键接口 |
|---|---|---|
| BACKBONE1~5 (RR/P) | 192.168.255.1~5 / 1921.6825.5001~5005 / 49.0001.…00 | 骨干环互联 |
| ISP1 (HEAD PE) | 192.168.255.11 / 1921.6825.5011 | GE0/0/0=200.147.10.17/29（绑 VPN） |
| ISP3 (DIVI PE) | 192.168.255.13 / 1921.6825.5013 | GE0/0/0=200.147.10.1/29（绑 VPN） |
| METRO_A1/A2/B1/B2 (P/PE) | 192.168.255.21~24 / 1921.6825.5021~5024 | 见下方城域接口表 |
| HEAD_FW1/FW2 (HA) | GE1/0/6=.18/.19，VRRP VIP=200.147.10.20 | NAT Server 200.147.10.21:80 → 10.1.1.1:80 |
| DIVI_FW | GE1/0/6=200.147.10.2/29 | NAT Server 200.147.10.2:80 → 192.168.151.10:80 |
| METRO_A1 GE0/0/3 | 200.147.50.1/24（绑 VPN enterprise） | 用户接入网关 |

**WAN 网段**：HEAD `200.147.10.16/29`、DIVI `200.147.10.0/29`、用户接入 `200.147.50.0/24`。
**服务器区**：HEAD `10.1.1.0/24`（HTTP1=10.1.1.1, HTTP2=10.1.1.2, DNS=.3/.4）；DIVI `192.168.151/152/153.0/24`。

---

## 5. 三大业务场景 / Scenarios

1. **总部 ↔ 分部 WAN 直连**：`HEAD_FW1` `ping -a 200.147.10.18 200.147.10.2`，纯 MPLS L3VPN，标签 push/swap/pop + PHP 全链路讲解（见 `HEAD_FW与DIVI_FW通信全过程.txt`）。
2. **站间加密**：GRE over IPSec，隧道 `192.168.0.0/30`，ACL 3000/3001 触发加密，业务流量封装为 IP→GRE→ESP→MPLS 四层。
3. **外部用户访问总部网站**：`USER_CLIENT1(200.147.50.10)` → MPLS VPN → HEAD_FW NAT Server → `http://200.147.10.21`（见 `USER_CLIENT经200.147.10.21访问总部HTTP全过程.txt`）。

---

## 6. 故障定位案例（高价值）/ Troubleshooting

**现象**：HEAD_FW 打不通 DIVI_FW。

**根因**：NOC 网段（`192.168.254.0/24`，ISP2↔ISP4）被宣告进 IS-IS 却**未配 MPLS/LDP**，形成一条非 MPLS 的 3 跳捷径，抢占 IS-IS 最短路径，导致 ISP1↔ISP3 之间的 LDP LSP 无法建立，VPN 数据面被黑洞。

**定位顺序**（逐层排查法）：
```
IS-IS 邻居 → LDP 会话/LSP → BGP VPNv4 路由 → VPN 实例路由表 → 端到端 ping
```

**修复**：把 ISP2/ISP4 的 GE0/0/2 从 IS-IS 中摘除；同时修正 BB5 GE0/0/1 与 GE0/0/2 配置与物理接线相反的问题。

---

## 7. 目录结构 / Directory Structure

```
enterprise_topo.topo                # 主拓扑文件（97 设备 / 124 链路）
运营商骨干网_ISIS配置.txt             # OSPF→IS-IS 迁移（13 台，含 NET 规划表）
城域网_4台P设备_重配命令.txt          # 城域 4 台 P 设备 IS-IS+MPLS 配置
城域网_用户接入_配置.txt              # Client_SW + 3 用户 PC 接入
用户访问HTTP_配置.txt                # 用户访问总部/分部 HTTP 方案（A/B 两套）
HEAD_FW与DIVI_FW通信全过程.txt       # 站间 MPLS VPN + GRE/IPSec 端到端讲解
USER_CLIENT经200.147.10.21访问总部HTTP全过程.txt  # 用户访问网站端到端讲解
结果表.txt                           # ISIS peer 实跑结果
```

---

## 8. 面试问答清单 / Interview Q&A

> 面试大概率从这里出题，建议逐条背熟（答案已对应本项目的真实配置）。

**Q1：为什么骨干用 IS-IS 而不是 OSPF？**
答：IS-IS 基于 CLNP，扩展性更强、收敛更快，且用 TLV 结构便于扩展（MPLS-TE 等），大型运营商骨干普遍选 IS-IS；本项目 `cost-style wide` 正是为承载 MPLS 标签/TE 做的扩展。二者在单区域 Level-2 场景能力接近，选择更多是工程习惯与扩展性考量。

**Q2：RD 和 RT 的区别是什么？**
答：RD（Route Distinguisher）解决**地址空间重叠**——把不同 VPN 的相同前缀变成全局唯一（本拓扑 HEAD=100:1、DIVI=100:3、用户接入=100:21，RD 不同）。RT（Route Target）是 BGP 扩展团体属性，控制**谁和谁互通**——本拓扑三个站点 RT 全部 100:1，所以能互访；RD 决定"唯一性"，RT 决定"连通性"。

**Q3：RR 解决什么问题？为什么 RR 上要 `undo policy vpn-target`？**
答：iBGP 全互联会导致 N² 邻居爆炸，RR（BACKBONE1/2）把路由反射给客户端（各 ISP、METRO），PE 只需与 RR 建邻居。默认 RR 会按 RT 过滤 VPNv4 路由，`undo policy vpn-target` 让 RR **不透传转发信息也不按 RT 丢弃**，把携带 RT 的 VPNv4 路由原样反射给所有客户端，避免 RT 不一致时路由被误丢。

**Q4：PHP（倒数第二跳弹出）是干嘛的？**
答：egress PE 向倒数第二跳通告 implicit-null（标签 3），倒数第二跳（如 METRO_A1/B1）转发时直接弹掉外层标签，只把内层标签交给出口 PE，省去出口 PE 查两次表（外层查 FIB + 内层查 VPN），减少转发负担。

**Q5：NAT Server 配了，为什么流量还是不通？**
答：缺 security-policy 放行。防火墙默认 zone 间拒绝，NAT Server 只做目的 NAT 生成 server-map，但 untrust→trust 方向仍需安全策略 `action permit`（本拓扑的 `untrust_to_trust_http` 就是踩坑后补的）。

**Q6：MPLS 数据面报文经过 P 节点时，P 会看内层 IP 吗？**
答：不会。P 节点只按**外层标签**做 SWAP（标签交换），不剥离外层、不读内层 IP，这是 MPLS 高速转发的核心——转发决定只在 ingress PE 做一次。

**Q7：三层标签栈里，内层标签是谁分的？外层呢？**
答：内层 VPN 标签由**出口 PE 经 MP-BGP** 通告（对端 ingress PE 压入）；外层传输标签由**LDP 沿 IGP 路径**逐跳分发（到出口 PE 的 Loopback FEC）。报文 = [外层 LDP 标签][内层 VPN 标签][原始 IP]。

**Q8：为什么用户接入网（GRT）和企业网站（VPN）原本隔离？怎么打通的？**
答：用户网在全局路由表（GRT，IS-IS 通告），企业 WAN 绑在 VPN 实例 `enterprise`，二者路由域隔离。方案：把 METRO_A1 升级为 PE，用户口 `GE0/0/3` `undo isis enable` 并 `ip binding vpn-instance enterprise`，用户网段经 BGP VPNv4 注入 VPN，成为第 3 个站点，与 HEAD/DIVI 同 RT 100:1 互通。

**Q9：GRE over IPSec 为什么要先用 GRE 再加密？**
答：IPSec 隧道模式原生适合承载单播 IP，不适合承载组播/非 IP；GRE 能把内网路由协议或任意 IP 流量先封装成点对点隧道，再交给 IPSec 加密。本拓扑先 GRE（`192.168.0.0/30`）建隧道、再 ACL 3000/3001 触发 ESP 加密，业务报文封装为 IP→GRE→ESP→MPLS。

**Q10：防火墙 VRRP HA 里，主备切换靠什么？VIP 是哪个？**
答：HEAD_FW1/FW2 在 GE1/0/6 配 `vrrp vrid 147`，虚拟 IP 200.147.10.20，主用（Active）持有 VIP 转发；备机监测主用状态，主用故障时抢占。NAT Server 与 IPSec 隧道源都引用 VIP，保证切换后业务不中断。

---

## 9. 如何运行 / How to Run

1. 安装 Huawei eNSP，导入 `enterprise_topo.topo`。
2. 按 `运营商骨干网_ISIS配置.txt` 依次下发 13 台路由器配置（先删 OSPF 再建 IS-IS）。
3. 按 `城域网_4台P设备_重配命令.txt` 配城域 4 台 P 设备。
4. 按 `用户访问HTTP_配置.txt` 完成 NAT Server + 安全策略 + METRO_A1 升级 PE。
5. 按各文档末尾的「验证命令」逐层验证：`display isis peer` → `display mpls ldp session` → `display bgp vpnv4 all routing-table` → `ping -vpn-instance enterprise 200.147.10.2`。

---

*本文档为作品集说明，配套配置与端到端过程见同目录各 txt 文件。*
