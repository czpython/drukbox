export class DurableObject {
  constructor(protected ctx: DurableObjectState, protected env: unknown) {}
}
