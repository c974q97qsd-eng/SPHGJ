import { createContext, useContext, useEffect, useState, type ReactNode } from "react"

type Theme = "dark" | "light"
interface ThemeCtx {
  theme: Theme
  toggle: () => void
  set: (t: Theme) => void
}

const Ctx = createContext<ThemeCtx>({ theme: "dark", toggle: () => {}, set: () => {} })

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setTheme] = useState<Theme>(() => {
    const saved = localStorage.getItem("pinlun-theme") as Theme | null
    return saved || "dark"
  })

  useEffect(() => {
    const root = document.documentElement
    root.classList.toggle("dark", theme === "dark")
    root.classList.toggle("light", theme === "light")
    localStorage.setItem("pinlun-theme", theme)
  }, [theme])

  return (
    <Ctx.Provider value={{
      theme,
      toggle: () => setTheme((t) => (t === "dark" ? "light" : "dark")),
      set: setTheme,
    }}>
      {children}
    </Ctx.Provider>
  )
}

export const useTheme = () => useContext(Ctx)
