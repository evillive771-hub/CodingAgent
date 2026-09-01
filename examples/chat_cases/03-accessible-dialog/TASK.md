# Accessible dialog requirement

完善登录弹窗的语义：

- 打开按钮通过 `aria-controls` 关联弹窗；
- 弹窗声明 `aria-modal="true"`，并通过 `aria-labelledby` 关联标题；
- 只有图标的关闭按钮提供 `aria-label="关闭登录弹窗"`。

验收命令：`node --test dialog.test.js`。
