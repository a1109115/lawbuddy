<!-- Maintained by Lu Lingyan, Deheng (Wuxi) Law Firm. -->
# 发布前必做（PUBLISH CHECKLIST）— 按需加载

> 仅 SkillHub 发布 / GitHub 推送 / 拷给他人前读取执行，日常阅卷不读取。

**案由知识库以分卷版随 skill 分发**（`references/cause-of-action-wiki/`，878 案由合并为 10 分卷 + toc_index，17 文件 ≤200 限制）。

**每次发布前必须核对**：

1. **脱敏复扫**：SKILL.md / references/ / templates/ / scripts/ 无真实案号、当事人姓名、密钥（grep 扫描见 skillhub-publisher 隐私清洗章节）
2. **知识库同步**：源 wiki（`~/.workbuddy/wiki-knowledge/民事案由指引_第三版/`）有更新时，重跑 `scripts/merge_cause_wiki.py <wiki根目录> references/cause-of-action-wiki` 重新生成分卷（自动自检 878 块 + 文件数 ≤200）
3. **文件数检查**：打包目录文件数 ≤200（references/ 全部 md + scripts + templates）
4. **路径一致性**：SKILL.md 知识库引用为 `references/cause-of-action-wiki/`（相对路径，随包分发）

📌 **版权合规**：知识库内容整理自人民法院出版社正版书籍（每个案由块标注书名/册/页码溯源），随包分发版已含 `DISCLAIMER.md`（"资料整理自公开网络，如有侵权请联系"）；`raw/` 上下册源文件不随包分发。
