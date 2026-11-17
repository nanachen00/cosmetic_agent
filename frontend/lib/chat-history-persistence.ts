type HistoryStorage = Pick<Storage, "setItem">;

/** Reuse immutable committed messages while the separate stream placeholder changes. */
export class ChatHistoryPersistence<Message extends object> {
  private savedStorage: HistoryStorage | null = null;
  private savedKey: string | null = null;
  private savedMessages: readonly Message[] = [];

  persist(
    storage: HistoryStorage,
    key: string,
    messages: readonly Message[],
    expiresAt: number,
  ): boolean {
    if (
      this.savedStorage === storage &&
      this.savedKey === key &&
      this.savedMessages.length === messages.length &&
      messages.every((message, index) => message === this.savedMessages[index])
    ) {
      return false;
    }

    const value = JSON.stringify({ version: 1, expiresAt, messages });
    storage.setItem(key, value);

    // A failed serialization/write must remain retryable on the next update.
    this.savedStorage = storage;
    this.savedKey = key;
    this.savedMessages = messages.slice();
    return true;
  }
}
