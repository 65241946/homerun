export const TRADING_ACTIVITY_REFRESH_MS = 30_000

type ReadOnlyRefetch = () => Promise<unknown>

export async function refreshTradingActivitySnapshots(
  refetchers: readonly ReadOnlyRefetch[],
): Promise<void> {
  await Promise.all(refetchers.map((refetch) => refetch()))
}
