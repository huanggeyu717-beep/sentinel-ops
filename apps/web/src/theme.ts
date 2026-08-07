// 主题偏好: auto 跟随系统 (CSS 的 prefers-color-scheme 兜底), light/dark 手动锁定。
// 只存偏好本身, 颜色全在 styles.css 的变量里。
import { useEffect } from 'react'
import { create } from 'zustand'
import { persist } from 'zustand/middleware'

export type Theme = 'auto' | 'light' | 'dark'

interface ThemeState {
  theme: Theme
  setTheme: (t: Theme) => void
}

export const useThemeStore = create<ThemeState>()(
  persist((set) => ({ theme: 'auto', setTheme: (theme) => set({ theme }) }), {
    name: 'sentinel-theme',
  }),
)

export const THEME_ORDER: Theme[] = ['auto', 'light', 'dark']
export const THEME_LABEL: Record<Theme, string> = {
  auto: '主题: 跟随系统',
  light: '主题: 浅色',
  dark: '主题: 深色',
}

export function useApplyTheme(): void {
  const theme = useThemeStore((s) => s.theme)
  useEffect(() => {
    const root = document.documentElement
    if (theme === 'auto') delete root.dataset.theme
    else root.dataset.theme = theme
  }, [theme])
}
