declare module "flv.js" {
  interface FlvPlayer {
    attachMediaElement(el: HTMLVideoElement): void
    load(): void
    play(): Promise<void>
    pause(): void
    destroy(): void
  }
  const flvjs: {
    isSupported(): boolean
    createPlayer(
      mediaDataSource: { type: string; url: string; isLive?: boolean },
      config?: Record<string, unknown>
    ): FlvPlayer
  }
  export default flvjs
}
