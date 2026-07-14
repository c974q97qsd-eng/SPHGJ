import { Toaster as Sonner } from "sonner"
import { useTheme } from "@/lib/theme"

/** 全局 toast。用法:import { toast } from "sonner"; toast.success("已保存") */
export function Toaster() {
  const { theme } = useTheme()
  return (
    <Sonner
      theme={theme}
      position="top-right"
      richColors
      closeButton
      toastOptions={{
        classNames: {
          toast: "rounded-lg border bg-card text-card-foreground shadow-lg",
        },
      }}
    />
  )
}
