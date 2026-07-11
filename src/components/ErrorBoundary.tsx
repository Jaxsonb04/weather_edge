import { Component, type ErrorInfo, type ReactNode } from "react";
import { ErrorState } from "./States";

interface ErrorBoundaryProps {
  children: ReactNode;
}

interface ErrorBoundaryState {
  error: Error | null;
}

export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("WeatherEdge view render failed", error, info.componentStack);
  }

  render() {
    if (this.state.error) return <ErrorState message={this.state.error.message} />;
    return this.props.children;
  }
}
