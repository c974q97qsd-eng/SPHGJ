import { Navigate } from "react-router-dom"

/** 「自动回复」已并入「评论管理」的页签(rev22)。保留此路由做旧链接兼容。 */
export function AutoReplyPage() {
  return <Navigate to="/comments?tab=reply" replace />
}
