import * as Sentry from '@sentry/nextjs';

const dsn = process.env.NEXT_PUBLIC_BETTER_STACK_ERRORS_DSN?.trim();
const sensitivePattern = /(bearer\s+[^\s]+|[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})/gi;

function redact(value: unknown): string {
  return String(value ?? '').replace(sensitivePattern, '[REDACTED]').slice(0, 1024);
}

if (dsn) {
  Sentry.init({
    dsn,
    environment: process.env.NEXT_PUBLIC_APP_ENV || process.env.NODE_ENV || 'development',
    release: process.env.NEXT_PUBLIC_APP_RELEASE,
    sendDefaultPii: false,
    tracesSampleRate: 0,
    replaysSessionSampleRate: 0,
    replaysOnErrorSampleRate: 0,
    maxBreadcrumbs: 10,
    integrations(defaultIntegrations) {
      return defaultIntegrations.filter(
        (integration) => !['BrowserTracing', 'Replay', 'BrowserProfiling'].includes(integration.name),
      );
    },
    beforeBreadcrumb(breadcrumb) {
      const category = breadcrumb.category || '';
      if (category === 'console' || category === 'fetch' || category === 'xhr' || category.startsWith('ui.')) {
        return null;
      }
      breadcrumb.message = redact(breadcrumb.message);
      breadcrumb.data = undefined;
      return breadcrumb;
    },
    beforeSend(event) {
      event.user = undefined;
      event.message = event.message ? redact(event.message) : event.message;
      if (event.request) {
        event.request.data = '[REDACTED]';
        event.request.cookies = undefined;
        event.request.headers = undefined;
        event.request.query_string = undefined;
      }
      event.breadcrumbs = event.breadcrumbs?.map((breadcrumb) => ({
        ...breadcrumb,
        data: undefined,
        message: breadcrumb.message ? redact(breadcrumb.message) : breadcrumb.message,
      }));
      event.exception?.values?.forEach((exception) => {
        exception.value = redact(exception.value);
      });
      return event;
    },
  });
}

export const onRouterTransitionStart = Sentry.captureRouterTransitionStart;
