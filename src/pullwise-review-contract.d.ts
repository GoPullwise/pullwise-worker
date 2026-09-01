declare module "pullwise-review-contract" {
  export function validateDefinition(
    definition: string,
    instance: unknown,
  ): Array<[path: string, message: string]>;
}
