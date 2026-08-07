export const fmtTime = (iso: string | null): string =>
  iso ? new Date(iso).toLocaleString('zh-CN', { hour12: false }) : '—'

export const fmtAge = (seconds: number | null): string => {
  if (seconds === null) return '从未上报'
  if (seconds < 60) return `${seconds} 秒前`
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`
  return `${Math.floor(seconds / 86400)} 天前`
}
