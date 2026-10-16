# 许可与来源说明

## 本项目许可

本项目原创代码、文档、配置与提示词采用根目录[LICENSE](LICENSE)的 **PolyForm Noncommercial License 1.0.0**。

- 允许：个人研究、实验、公开知识测试、个人学习、私人娱乐、业余爱好项目；慈善机构、教育机构、公共研究机构、公共安全/卫生机构、环保组织与政府机构的使用；以及为上述目的修改、衍生与再分发（须随附本许可全文或链接）。
- 禁止：以商业优势或金钱补偿为主要目的的使用；企业内部商业运营中的使用；作为付费或托管服务、托管 API、商业 SaaS 对外提供。上述用途需另行取得著作权人的书面商业许可。
- 该许可**不是** OSI 认可的开源许可，本项目**不是**开源软件，而是「源码公开、仅供非商业用途」。

著作权人：王庆福 / Wang Qingfu（GitHub: [leo372239-1024](https://github.com/leo372239-1024)），Copyright (C) 2026。商业授权的申请方式与允许/禁止用途清单见[商业授权说明](COMMERCIAL.md)。

注：PolyForm 系列许可未被 GitHub 的自动许可识别体系收录，仓库页面的 License 标签会显示为 "Other"，这不影响 [LICENSE](LICENSE) 的法律效力，请以仓库根目录的 LICENSE 正文为准。

## 第三方来源与许可边界

第三方组件、服务、基础镜像与接口各自保留其许可和使用条件，本项目许可不改变、也不覆盖其条款。

规模路由实验的960个外部函数描述来自Gorilla/Berkeley Function Calling Leaderboard，固定commit `6ea57973c7a6097fd7c5915698c54c17c5b1b6c8`。仅规范化类型与函数名称构成只读路由目录，保留描述，不执行外部函数，不表示取得官方BFCL分数。原许可证副本：[BFCL-LICENSE](docs/evaluation/BFCL-LICENSE.txt)。目录来源：[bfcl-tool-catalog-v1.json](docs/evaluation/bfcl-tool-catalog-v1.json)。上游：[Gorilla](https://github.com/ShishirPatil/gorilla)。

依赖由锁文件或镜像digest指定，遵循上游许可。Elasticsearch发布镜像采用相应Elastic许可。本项目没有复制Dify或Salesforce Agentforce的受限制业务实现；架构比较不构成商业质量的实测优越性结论。
