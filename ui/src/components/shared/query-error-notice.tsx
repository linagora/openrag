import { AlertCircle, RefreshCw } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";

export function QueryErrorNotice({
  title,
  message,
  isRetrying,
  onRetry,
  stale = false,
}: {
  title: string;
  message: string;
  isRetrying: boolean;
  onRetry: () => void;
  stale?: boolean;
}) {
  return (
    <Alert variant={stale ? "default" : "destructive"}>
      <AlertCircle aria-hidden="true" />
      <AlertTitle>{title}</AlertTitle>
      <AlertDescription>
        <p>{message}</p>
        <Button type="button" variant="outline" size="sm" onClick={onRetry} disabled={isRetrying}>
          <RefreshCw className={isRetrying ? "animate-spin" : ""} aria-hidden="true" />
          {isRetrying ? "Retrying…" : "Retry"}
        </Button>
      </AlertDescription>
    </Alert>
  );
}
