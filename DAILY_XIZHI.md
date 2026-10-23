# 美元兑人民币每日息知推送

每日北京时间09:00运行`.github/workflows/usd_cny_daily.yml`，读取中国银行美元牌价并向息知发送一份日报。GitHub定时任务可能排队延迟。

## 配置

1. 在当前仓库打开Settings → Secrets and variables → Actions → Secrets。
2. 添加Repository Secret，名称为`XZKEY`，值为息知Key本身。不要填写完整网址，不要保存到Variables或代码中。
3. 其他仓库的Repository Secret不会自动共享。可以使用签到仓库已经验证成功的同一个息知Key，但需要在本仓库单独添加。
4. Actions → USD-CNY Daily XiZhi → Run workflow，选择main，测试微信实际接收情况。

只使用Python标准库，不需要安装Server酱SDK。密钥仅从`XZKEY`环境变量读取。每次普通运行最多发送一条日报或失败提醒；手动运行是额外的一次，计划任务仍会运行。息知请求使用HTTPS、20秒超时，遇到不确定的网络失败不自动重发，以减少重复通知。

日报保留现汇买入价、现钞买入价、现汇卖出价、现钞卖出价、报价时间和来源链接。中行原表的每100美元牌价已除以100，消息显示每1美元折合人民币的金额。

## 本地验证

```bash
python usd_cny_daily.py --dry-run
python -m unittest discover -s tests -v
```

预览不读取密钥、不推送。行情获取或解析失败时，会尝试发送一条失败提醒，进程仍返回失败状态；推送失败时也返回失败状态。息知接口接收请求不等于微信最终送达。

原项目的邮件和阈值监控脚本未接入此入口。此说明对应`USD-CNY Daily XiZhi`工作流。
